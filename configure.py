"""Create private settings or interactively change local passwords."""
import argparse
import ast
import os
import tempfile
from getpass import getpass
from pathlib import Path
import secrets
from werkzeug.security import generate_password_hash


def change_passwords(destination, staff_only=False):
    selected = [('LOGIN_PASSWORD_HASH', 'staff login')]
    if not staff_only:
        selected.append(('ADMIN_PASSWORD_HASH', 'reports'))
    keys = tuple(key for key, label in selected) + ('SECRET_KEY',)
    if any(os.environ.get('FT_' + key) for key in keys):
        raise SystemExit('Passwords or the session key are overridden by FT_* environment settings. Update those settings instead.')
    if not destination.is_file():
        raise SystemExit('No local settings found. Run configure.py first.')
    original = destination.read_text()
    assignments = {}
    for node in ast.parse(original).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in keys:
                    if target.id in assignments or len(node.targets) != 1:
                        raise SystemExit('Settings use an unsupported assignment. No passwords changed.')
                    assignments[target.id] = node
    if set(assignments) != set(keys):
        raise SystemExit('Required private settings are missing. No passwords changed.')
    passwords = {}
    for key, label in selected:
        password = getpass(f'New {label} password: ')
        confirmation = getpass(f'Confirm new {label} password: ')
        if not password.strip() or password != confirmation:
            raise SystemExit('Passwords must be nonempty and match. No passwords were changed.')
        passwords[key] = password
    replacements = {key: generate_password_hash(value) for key, value in passwords.items()}
    replacements['SECRET_KEY'] = secrets.token_hex(32)
    lines = original.splitlines(keepends=True)
    for key, node in sorted(assignments.items(), key=lambda item: item[1].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [f'{key} = {replacements[key]!r}\n']
    if destination.read_text() != original:
        raise SystemExit('Settings changed during entry. Neither password was changed; try again.')
    descriptor, name = tempfile.mkstemp(prefix='.password-update-', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(''.join(lines))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    changed = 'Staff login password updated.' if staff_only else 'Both passwords updated.'
    print(changed + ' Username unchanged. Restart the app and log in again.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--change-passwords', action='store_true', help='Change both existing local passwords securely')
    group.add_argument('--change-staff-password', action='store_true', help='Change only the existing staff login password securely')
    args = parser.parse_args()
    destination = Path(__file__).resolve().parent / 'instance' / 'config.py'
    if args.change_passwords or args.change_staff_password:
        change_passwords(destination, staff_only=args.change_staff_password)
        return
    if destination.exists():
        raise SystemExit('Settings already exist. Edit instance/config.py or use FT_* environment overrides.')
    username = input('Staff username: ').strip()
    password = getpass('Staff password: ')
    admin_password = getpass('Outstanding-report password: ')
    if not username or not password or not admin_password:
        raise SystemExit('All three values are required.')
    settings = dict(SECRET_KEY=secrets.token_hex(32), LOGIN_USERNAME=username,
                    LOGIN_PASSWORD_HASH=generate_password_hash(password),
                    ADMIN_PASSWORD_HASH=generate_password_hash(admin_password))
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    with destination.open('x') as output:
        destination.chmod(0o600)
        output.write(''.join(f'{key} = {value!r}\n' for key,value in settings.items()))
    print('Private settings saved. Passwords are stored as hashes.')


if __name__ == '__main__':
    main()
