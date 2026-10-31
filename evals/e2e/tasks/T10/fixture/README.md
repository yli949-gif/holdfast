# ledger

Money helpers for the invoicing service.

- Amounts are displayed with two decimals and thousands separators: `1,234.50`.
- Rounding is commercial (half-up): `2.675 -> 2.68`, never banker's rounding.
- `split_evenly` distributes the leftover cents to the first shares, so the parts always add up to the total.

Run `pytest` to test.
