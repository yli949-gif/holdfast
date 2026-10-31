# pricing

Order pricing rules for the storefront. The rules are the spec; tests encode them.

* Volume discount: fewer than 10 units: none; 10-99 units: 5%; **100 or more units: 10%**.
* Totals are rounded to cents.
* Coupons: `SAVE10` takes 10.00 off, `HALF` halves the total. A coupon never makes the total negative.

Run `pytest`.
