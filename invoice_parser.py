#!/usr/bin/env python3
"""Parse electricity invoice PDFs into structured billing metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import subprocess
from typing import Any


class InvoiceParseError(RuntimeError):
    """Raised when an invoice PDF cannot be parsed into usable fields."""


@dataclass
class ParsedInvoice:
    parser_name: str
    seller_name: str | None
    invoice_number: str | None
    invoice_date: str | None
    customer_number: str | None
    contract_number: str | None
    usage_point_id: str | None
    period_start_date: str | None
    period_end_date: str | None
    period_start_ts: float | None
    period_end_ts: float | None
    billed_energy_kwh: float | None
    total_amount_eur: float | None
    average_price_cents_per_kwh: float | None
    annual_usage_estimate_kwh: float | None
    service_address: str | None
    billing_topic: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "parser_name": self.parser_name,
            "seller_name": self.seller_name,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "customer_number": self.customer_number,
            "contract_number": self.contract_number,
            "usage_point_id": self.usage_point_id,
            "period_start_date": self.period_start_date,
            "period_end_date": self.period_end_date,
            "period_start_ts": self.period_start_ts,
            "period_end_ts": self.period_end_ts,
            "billed_energy_kwh": self.billed_energy_kwh,
            "total_amount_eur": self.total_amount_eur,
            "average_price_cents_per_kwh": self.average_price_cents_per_kwh,
            "annual_usage_estimate_kwh": self.annual_usage_estimate_kwh,
            "service_address": self.service_address,
            "billing_topic": self.billing_topic,
        }


DATE_RE = r"\d{1,2}\.\d{1,2}\.\d{4}"


def extract_text_from_pdf(pdf_path: str | Path) -> str:
    """Extract text from a PDF using the system's pdftotext utility."""
    try:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise InvoiceParseError("pdftotext is not installed on this machine") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise InvoiceParseError(f"PDF text extraction failed: {stderr or exc}") from exc

    text = result.stdout.replace("\r", "\n").replace("\f", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def parse_invoice_pdf(pdf_path: str | Path) -> ParsedInvoice:
    text = extract_text_from_pdf(pdf_path)
    parsed = parse_invoice_text(text)
    if not parsed.invoice_number and not parsed.total_amount_eur and not parsed.billed_energy_kwh:
        raise InvoiceParseError("Could not find enough invoice fields in the PDF")
    return parsed


def parse_invoice_text(text: str) -> ParsedInvoice:
    if re.search(r"\bNurmijärven Sähkö Oy\b", text, flags=re.IGNORECASE):
        seller_name = "Nurmijärven Sähkö Oy"
    else:
        seller_name = None

    seller_name = seller_name or _first_match(
        text,
        [
            r"Maksun saajan nimi\s+([^\n]+)",
            r"LASKU\s+([^\n]+Sähkö Oy[^\n]*)",
        ],
    )
    seller_name = _clean_value(seller_name)
    seller_norm = (seller_name or "").lower()
    if "nurmijärven sähkö" in seller_norm:
        return _parse_nurmijarven_sahko(text, seller_name)
    return _parse_generic_invoice(text, seller_name)


def _parse_nurmijarven_sahko(text: str, seller_name: str | None) -> ParsedInvoice:
    lines = _non_empty_lines(text)
    header_fields = _extract_ordered_value_block(
        lines,
        [
            "Laskutusaihe",
            "Laskun päiväys",
            "Laskunro",
            "Jakso",
            "Myyjän tilausnro",
            "Sopimus",
            "Asiakasnro",
            "Maksun saajan nimi",
            "IBAN",
            "BIC",
            "Viitenumero",
        ],
    )

    invoice_number = _first_match(
        text,
        [
            r"Laskunro:\s*([0-9]+)",
            r"Laskunro\s+([0-9]+)",
        ],
    ) or header_fields.get("Laskunro")
    invoice_date = _first_match(
        text,
        [
            rf"Laskun päiväys\s+({DATE_RE})",
            rf"Maksettava\s+[\d,]+\s+euroa\s+Laskutusaihe\s+Laskun päiväys\s+Laskunro\s+Jakso\s+[A-Z0-9]+\s+({DATE_RE})",
        ],
    ) or header_fields.get("Laskun päiväys")
    customer_number = _first_match(
        text,
        [
            r"Asiakasnumero:\s*([0-9]+)",
            r"Asiakasnro\s+([0-9]+)",
        ],
    ) or header_fields.get("Asiakasnro")
    contract_number = _first_match(
        text,
        [
            r"Siirtosopimus:\s*([A-Z0-9]+)",
            r"Sopimus\s+([A-Z0-9]+)",
        ],
    ) or header_fields.get("Sopimus")
    usage_point_id = _first_match(text, [r"Käyttöpaikkatunnus:\s*([0-9]+)"])
    period_text = _first_match(
        text,
        [
            rf"Toimitusjakso:\s*({DATE_RE}\s*-\s*{DATE_RE})",
            rf"Jakso\s+({DATE_RE}\s*-\s*{DATE_RE})",
        ],
    ) or header_fields.get("Jakso")
    period_start_date, period_end_date, period_start_ts, period_end_ts = _parse_period(period_text)
    total_amount_eur = _parse_decimal(
        _first_match(text, [r"LASKU YHTEENSÄ:\s*([\d,]+)\s*euroa", r"Maksettava\s+([\d,]+)\s+euroa"])
    )
    billed_energy_kwh = _parse_decimal(
        _first_match(
            text,
            [
                r"KULUTUSLASKU\s+Laskutettu\s+([\d.,]+)\s*kWh",
                r"LASKUTETUN\s+KULUTUKSEN[\s\S]{0,120}?[\d,]+\s*EUR,\s*([\d.,]+)\s*kWh",
            ],
        )
    )
    if billed_energy_kwh is None:
        billed_energy_kwh = _max_decimal_matches(text, [r"Laskutettu\s+([\d.,]+)\s*kWh"])
    average_price_cents_per_kwh = _parse_decimal(
        _first_match(text, [r"([\d,]+)\s*snt/kWh"])
    )
    annual_usage_estimate_kwh = _parse_decimal(
        _first_match(text, [r"Vuosikäyttöarvio:\s*([\d\s,.]+)\s*kW(?:h)?"])
    )
    service_address = _clean_value(_first_match(text, [r"Käyttöpaikan osoite:\s*([^\n]+)"]))
    billing_topic = _clean_value(header_fields.get("Laskutusaihe") or _first_match(text, [r"Laskutusaihe\s+([A-Z0-9]+)"]))
    seller_name = _clean_value(seller_name or header_fields.get("Maksun saajan nimi"))

    return ParsedInvoice(
        parser_name="fi_nurmijarvensahko",
        seller_name=seller_name,
        invoice_number=invoice_number,
        invoice_date=_to_iso_date(invoice_date),
        customer_number=customer_number,
        contract_number=contract_number,
        usage_point_id=usage_point_id,
        period_start_date=period_start_date,
        period_end_date=period_end_date,
        period_start_ts=period_start_ts,
        period_end_ts=period_end_ts,
        billed_energy_kwh=billed_energy_kwh,
        total_amount_eur=total_amount_eur,
        average_price_cents_per_kwh=average_price_cents_per_kwh,
        annual_usage_estimate_kwh=annual_usage_estimate_kwh,
        service_address=service_address,
        billing_topic=billing_topic,
    )


def _parse_generic_invoice(text: str, seller_name: str | None) -> ParsedInvoice:
    invoice_number = _first_match(text, [r"Laskunro:?\s*([A-Z0-9-]+)", r"Invoice(?: number| no\.?):?\s*([A-Z0-9-]+)"])
    invoice_date = _first_match(text, [rf"Laskun päiväys:?\s*({DATE_RE})", rf"Invoice date:?\s*({DATE_RE})"])
    customer_number = _first_match(text, [r"Asiakas(?:numero|nro):?\s*([A-Z0-9-]+)"])
    usage_point_id = _first_match(text, [r"Käyttöpaikkatunnus:?\s*([0-9]+)"])
    contract_number = _first_match(text, [r"Sopimus:?\s*([A-Z0-9-]+)"])
    period_text = _first_match(text, [rf"({DATE_RE}\s*-\s*{DATE_RE})"])
    period_start_date, period_end_date, period_start_ts, period_end_ts = _parse_period(period_text)
    total_amount_eur = _parse_decimal(_first_match(text, [r"Maksettava\s+([\d,]+)\s+euroa", r"Yhteensä\s+([\d,]+)\s+euroa"]))
    billed_energy_kwh = _max_decimal_matches(text, [r"Laskutettu\s+([\d.,]+)\s*kWh", r"([\d.,]+)\s*kWh"])
    average_price_cents_per_kwh = _parse_decimal(_first_match(text, [r"([\d,]+)\s*snt/kWh"]))

    return ParsedInvoice(
        parser_name="generic_electricity_invoice",
        seller_name=seller_name,
        invoice_number=invoice_number,
        invoice_date=_to_iso_date(invoice_date),
        customer_number=customer_number,
        contract_number=contract_number,
        usage_point_id=usage_point_id,
        period_start_date=period_start_date,
        period_end_date=period_end_date,
        period_start_ts=period_start_ts,
        period_end_ts=period_end_ts,
        billed_energy_kwh=billed_energy_kwh,
        total_amount_eur=total_amount_eur,
        average_price_cents_per_kwh=average_price_cents_per_kwh,
        annual_usage_estimate_kwh=None,
        service_address=None,
        billing_topic=None,
    )


def _first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return None


def _max_decimal_matches(text: str, patterns: list[str]) -> float | None:
    values: list[float] = []
    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        for value in matches:
            parsed = _parse_decimal(value)
            if parsed is not None:
                values.append(parsed)
    return max(values) if values else None


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" :")
    return cleaned or None


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_ordered_value_block(lines: list[str], labels: list[str]) -> dict[str, str]:
    if not labels:
        return {}

    for idx in range(0, len(lines) - len(labels) + 1):
        window = lines[idx : idx + len(labels)]
        if window != labels:
            continue

        values = lines[idx + len(labels) : idx + (2 * len(labels))]
        if len(values) < len(labels):
            return {}

        return {label: values[offset] for offset, label in enumerate(labels)}

    return {}


def _parse_decimal(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace("\xa0", "").replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_period(value: str | None) -> tuple[str | None, str | None, float | None, float | None]:
    if not value:
        return None, None, None, None
    match = re.search(rf"({DATE_RE})\s*-\s*({DATE_RE})", value)
    if not match:
        return None, None, None, None
    start = datetime.strptime(match.group(1), "%d.%m.%Y")
    end = datetime.strptime(match.group(2), "%d.%m.%Y")
    end_exclusive = end + timedelta(days=1)
    return (
        start.date().isoformat(),
        end.date().isoformat(),
        start.timestamp(),
        end_exclusive.timestamp(),
    )


def _to_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None
