"""Synthetic XBRL 2.1 instances for tests.

These are NOT copies of any exchange filing (none was available offline). They follow the XBRL 2.1
instance structure (contexts, units, facts, explicit/typed dimensions) with concept names from the
tables in `xbrl_map.py`, so the reader's behaviour is tested, but a real NSE/BSE file has not been
read. Amounts are in Lakhs unless the test says otherwise.
"""

NS = (
    'xmlns="http://www.xbrl.org/2003/instance" '
    'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:link="http://www.xbrl.org/2003/linkbase" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
    'xmlns:in-capmkt="https://www.sebi.gov.in/xbrl/2020/in-capmkt" '
    'xmlns:in-bse-fin="https://www.bseindia.com/xbrl/fin/2020/in-bse-fin"'
)
Q2_CUR = ("2026-07-01", "2026-09-30")
Q2_PRI = ("2025-07-01", "2025-09-30")
H1_CUR = ("2026-04-01", "2026-09-30")


def duration(cid, start, end, ident="RELIANCE", dims=""):
    seg = f"<xbrli:segment>{dims}</xbrli:segment>" if dims else ""
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity>'
        f'<xbrli:identifier scheme="http://www.bseindia.com">{ident}</xbrli:identifier>{seg}'
        f"</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
        f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def instant(cid, on, ident="RELIANCE", dims=""):
    seg = f"<xbrli:segment>{dims}</xbrli:segment>" if dims else ""
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity>'
        f'<xbrli:identifier scheme="http://www.bseindia.com">{ident}</xbrli:identifier>{seg}'
        f"</xbrli:entity><xbrli:period><xbrli:instant>{on}</xbrli:instant></xbrli:period>"
        "</xbrli:context>"
    )


UNITS = (
    '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
    '<xbrli:unit id="PCT"><xbrli:measure>xbrli:percent</xbrli:measure></xbrli:unit>'
    '<xbrli:unit id="PURE"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>'
    '<xbrli:unit id="INRPS"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:INR'
    "</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares"
    "</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>"
)


def amount(concept, ctx, value, decimals="2", prefix="in-capmkt"):
    return (
        f'<{prefix}:{concept} contextRef="{ctx}" unitRef="INR" decimals="{decimals}">'
        f"{value}</{prefix}:{concept}>"
    )


def text(concept, ctx, value, prefix="in-capmkt"):
    return f'<{prefix}:{concept} contextRef="{ctx}">{value}</{prefix}:{concept}>'


def wrap(*parts, schema="in-capmkt-2020-04-01.xsd"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<xbrli:xbrl {NS}>"
        f'<link:schemaRef xlink:type="simple" xlink:href="{schema}"/>'
        + "".join(parts)
        + "</xbrli:xbrl>"
    ).encode()


def meta(basis="Consolidated", rounding="Lakhs", ctx="cur", audited="Unaudited"):
    out = []
    if basis:
        out.append(text("NatureOfReportStandaloneConsolidated", ctx, basis))
    if rounding:
        out.append(text("LevelOfRoundingUsedInFinancialStatements", ctx, rounding))
    if audited:
        out.append(text("WhetherResultsAreAuditedOrUnaudited", ctx, audited))
    return "".join(out)


def indas_income(ctx="cur", *, revenue=1000, other=50, total=1050, expenses=800, pbt=250,
                 tax=60, profit=190, decimals="2"):  # fmt: skip
    return "".join(
        amount(c, ctx, v, decimals)
        for c, v in (
            ("RevenueFromOperations", revenue),
            ("OtherIncome", other),
            ("Income", total),
            ("Expenses", expenses),
            ("ProfitBeforeTax", pbt),
            ("TaxExpense", tax),
            ("ProfitLossForPeriod", profit),
        )
    )


def indas_filing(*, basis="Consolidated", rounding="Lakhs", ident="RELIANCE", income=None,
                 extra="", decimals="2", audited="Unaudited"):  # fmt: skip
    return wrap(
        duration("cur", *Q2_CUR, ident=ident),
        duration("pri", *Q2_PRI, ident=ident),
        instant("end", "2026-09-30", ident=ident),
        UNITS,
        meta(basis, rounding, audited=audited),
        income if income is not None else indas_income("cur", decimals=decimals),
        indas_income(
            "pri",
            revenue=900,
            other=40,
            total=940,
            expenses=720,
            pbt=220,
            tax=50,
            profit=170,
            decimals=decimals,
        ),  # fmt: skip
        amount("Assets", "end", 5000, decimals),
        amount("EquityAndLiabilities", "end", 5000, decimals),
        amount("Equity", "end", 2000, decimals),
        extra,
    )


def bank_filing(*, basis="Standalone", rounding="Lakhs", ident="RELIANCE", ratios="percent",
                extra=""):  # fmt: skip
    unit = {"percent": "PCT", "pure": "PURE"}[ratios]

    def ratio(concept, value):
        return (
            f'<in-bse-fin:{concept} contextRef="cur" unitRef="{unit}" decimals="2">{value}'
            f"</in-bse-fin:{concept}>"
        )

    return wrap(
        duration("cur", *Q2_CUR, ident=ident),
        instant("end", "2026-09-30", ident=ident),
        UNITS,
        meta(basis, rounding),
        amount("InterestEarned", "cur", 2000, prefix="in-bse-fin"),
        amount("OtherIncome", "cur", 300, prefix="in-bse-fin"),
        amount("Income", "cur", 2300, prefix="in-bse-fin"),
        amount("InterestExpended", "cur", 1100, prefix="in-bse-fin"),
        amount("OperatingExpenses", "cur", 400, prefix="in-bse-fin"),
        amount("Advances", "end", 30000, prefix="in-bse-fin"),
        amount("Deposits", "end", 35000, prefix="in-bse-fin"),
        ratio("PercentageOfGrossNpa", "1.16"),
        ratio("PercentageOfNpa", "0.35"),
        ratio("CapitalAdequacyRatio", "16.27"),
        extra,
        schema="in-bse-fin-banking.xsd",
    )
