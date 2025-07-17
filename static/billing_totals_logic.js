
<script>
function updateTotals() {
    let total = 0;
    let qtyInputs = document.querySelectorAll("input[name='qty[]']");
    let rateInputs = document.querySelectorAll("input[name='rate[]']");
    let totalInputs = document.querySelectorAll("input[name='total[]']");

    for (let i = 0; i < qtyInputs.length; i++) {
        let qty = parseFloat(qtyInputs[i].value) || 0;
        let rate = parseFloat(rateInputs[i].value) || 0;
        let lineTotal = qty * rate;
        totalInputs[i].value = lineTotal.toFixed(2);
        total += lineTotal;
    }

    // Apply Discount
    let discountVal = parseFloat(document.querySelector("input[name='discountValue']").value) || 0;
    let discountType = document.querySelector("select[name='discountType']").value;
    let discountedAmount = (discountType === '%') ? total * (discountVal / 100) : discountVal;
    let grandTotal = Math.max(0, total - discountedAmount);

    // Update total field
    document.querySelector("input[name='totalAmount']").value = grandTotal.toFixed(2);

    // Apply Advance & Balance
    let advance = parseFloat(document.querySelector("input[name='advancePaid']").value) || 0;
    let balance = Math.max(0, grandTotal - advance);
    document.querySelector("input[name='balanceAmount']").value = balance.toFixed(2);
}

// Bind all necessary inputs
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[name='qty[]'], input[name='rate[]']").forEach(input =>
        input.addEventListener("input", updateTotals)
    );
    document.querySelector("input[name='discountValue']").addEventListener("input", updateTotals);
    document.querySelector("select[name='discountType']").addEventListener("change", updateTotals);
    document.querySelector("input[name='advancePaid']").addEventListener("input", updateTotals);

    // Also update totals after page load
    updateTotals();
});
</script>
