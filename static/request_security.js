// Keep existing forms, fetch calls, and jQuery requests protected without changing their UI.
(() => {
  const token = document.querySelector('meta[name="csrf-token"]').content;
  const unsafe = method => !['GET', 'HEAD', 'OPTIONS'].includes((method || 'GET').toUpperCase());
  const sameOrigin = url => new URL(url, location.href).origin === location.origin;
  const originalFetch = window.fetch;
  window.fetch = function (input, options = {}) {
    const url = input instanceof Request ? input.url : input;
    const method = options.method || (input instanceof Request ? input.method : 'GET');
    if (sameOrigin(url) && unsafe(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('X-CSRF-Token', token);
      options = {...options, headers};
    }
    return originalFetch.call(this, input, options);
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.ftNeedsToken = unsafe(method) && sameOrigin(url);
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    if (this.ftNeedsToken) this.setRequestHeader('X-CSRF-Token', token);
    return send.apply(this, args);
  };
  document.addEventListener('submit', event => {
    const form = event.target;
    if (unsafe(form.method) && sameOrigin(form.action)) {
      let field = form.querySelector('input[name="csrf_token"]');
      if (!field) {
        field = document.createElement('input');
        field.type = 'hidden';
        field.name = 'csrf_token';
        form.appendChild(field);
      }
      field.value = token;
    }
  }, true);
})();
