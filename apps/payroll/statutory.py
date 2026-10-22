"""Kenyan statutory payroll deductions (monthly).

Rates live in one place so they can be updated when the law changes.
Check them against KRA / NSSF / SHA notices before each tax year.

PAYE bands (Finance Act 2023), personal relief 2,400.
NSSF (NSSF Act 2013, phase from Feb 2025): 6% employee + 6% employer on
pensionable pay up to the upper earnings limit.
SHIF (Social Health Insurance Act 2023): 2.75% of gross, minimum 300.
Affordable Housing Levy: 1.5% employee + 1.5% employer.
Since 27 Dec 2024 NSSF, SHIF and the housing levy are deducted before PAYE
(Tax Laws (Amendment) Act 2024).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
CENT = D("0.01")

PAYE_BANDS = [            # (upper limit of band, rate)
    (D("24000"), D("0.10")),
    (D("32333"), D("0.25")),
    (D("500000"), D("0.30")),
    (D("800000"), D("0.325")),
    (None, D("0.35")),
]
PERSONAL_RELIEF = D("2400")
NSSF_RATE = D("0.06")
NSSF_UPPER_LIMIT = D("72000")
SHIF_RATE = D("0.0275")
SHIF_MINIMUM = D("300")
HOUSING_LEVY_RATE = D("0.015")


def _money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def paye(taxable: Decimal) -> Decimal:
    tax, lower = D("0"), D("0")
    for upper, rate in PAYE_BANDS:
        if taxable <= lower:
            break
        top = taxable if upper is None else min(taxable, upper)
        tax += (top - lower) * rate
        if upper is None:
            break
        lower = upper
    return _money(max(tax - PERSONAL_RELIEF, D("0")))


@dataclass
class Payslip:
    gross: Decimal
    nssf: Decimal
    shif: Decimal
    housing_levy: Decimal
    taxable: Decimal
    paye: Decimal
    net: Decimal
    employer_nssf: Decimal
    employer_housing_levy: Decimal

    @property
    def deductions(self) -> Decimal:
        return self.nssf + self.shif + self.housing_levy + self.paye


def compute(gross: Decimal) -> Payslip:
    gross = _money(D(gross))
    nssf = _money(min(gross, NSSF_UPPER_LIMIT) * NSSF_RATE)
    shif = _money(max(gross * SHIF_RATE, SHIF_MINIMUM)) if gross > 0 else D("0")
    levy = _money(gross * HOUSING_LEVY_RATE)
    taxable = max(gross - nssf - shif - levy, D("0"))
    tax = paye(taxable)
    return Payslip(gross=gross, nssf=nssf, shif=shif, housing_levy=levy, taxable=taxable, paye=tax,
                   net=gross - nssf - shif - levy - tax, employer_nssf=nssf, employer_housing_levy=levy)
