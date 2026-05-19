"""
Unit tests for nordea_parser.py

Coverage:
- _clean_amount:  Danish format, negatives, None/empty edge cases
- categorize:     known merchant, unknown, case-insensitive, None fields, override precedence
- _normalise:     happy path, missing required columns
- parse_csv_file: minimal synthetic CSV via tmp_path fixture
"""

import io
import textwrap

import pandas as pd
import pytest

from nordea_parser import (
    CATEGORIES,
    _clean_amount,
    _normalise,
    categorize,
    parse_csv_file,
)


# ── _clean_amount ─────────────────────────────────────────────────────────────

class TestCleanAmount:
    def test_danish_thousands_and_decimal(self):
        assert _clean_amount("1.234,56") == pytest.approx(1234.56)

    def test_negative_danish_format(self):
        assert _clean_amount("-2.000,00") == pytest.approx(-2000.0)

    def test_plain_integer_string(self):
        assert _clean_amount("500") == pytest.approx(500.0)

    def test_dot_treated_as_thousands_separator(self):
        # Parser strips all dots (Danish thousands separator), so "49.95" → 4995.0
        assert _clean_amount("49.95") == pytest.approx(4995.0)

    def test_none_returns_none(self):
        assert _clean_amount(None) is None

    def test_empty_string_returns_none(self):
        assert _clean_amount("") is None

    def test_whitespace_only_returns_none(self):
        assert _clean_amount("   ") is None

    def test_string_none_returns_none(self):
        assert _clean_amount("None") is None

    def test_non_numeric_returns_none(self):
        assert _clean_amount("ikke et tal") is None

    def test_nbsp_stripped(self):
        # Nordea sometimes uses non-breaking space as thousands separator
        assert _clean_amount("1\xa0000,00") == pytest.approx(1000.0)

    def test_positive_amount(self):
        assert _clean_amount("12.345,67") == pytest.approx(12345.67)


# ── categorize ────────────────────────────────────────────────────────────────

RULES = dict(CATEGORIES)

def _row(navn=None, beskrivelse=None, modtager=None, afsender=None, dato=None, beløb=None, label=None):
    return {
        "navn": navn,
        "beskrivelse": beskrivelse,
        "modtager": modtager,
        "afsender": afsender,
        "dato": pd.Timestamp("2024-01-15") if dato is None else dato,
        "beløb": beløb or -100.0,
        "label": label or (navn or beskrivelse or ""),
    }


class TestCategorize:
    def test_known_merchant_via_navn(self):
        row = _row(navn="Netto Amager")
        assert categorize(row, RULES) == "Dagligvarer"

    def test_known_merchant_case_insensitive(self):
        row = _row(navn="REMA 1000")
        assert categorize(row, RULES) == "Dagligvarer"

    def test_keyword_in_beskrivelse(self):
        row = _row(beskrivelse="DSB Rejsekort optankning")
        assert categorize(row, RULES) == "Transport"

    def test_keyword_in_modtager(self):
        row = _row(modtager="Netflix")
        assert categorize(row, RULES) == "Abonnementer"

    def test_unknown_merchant_returns_andet(self):
        row = _row(navn="Jobbygejser ApS")
        assert categorize(row, RULES) == "Andet"

    def test_none_fields_do_not_crash(self):
        row = _row(navn=None, beskrivelse=None, modtager=None, afsender=None)
        assert categorize(row, RULES) == "Andet"

    def test_override_wins_over_keyword(self):
        row = _row(navn="Netto Amager", label="Netto Amager", beløb=-99.0)
        row["dato"] = pd.Timestamp("2024-01-15")
        key = "2024-01-15||-99.0||Netto Amager"
        overrides = {key: "Børn & Familie"}
        assert categorize(row, RULES, overrides) == "Børn & Familie"

    def test_no_override_match_falls_through_to_keyword(self):
        row = _row(navn="Netto Amager", label="Netto Amager", beløb=-99.0)
        row["dato"] = pd.Timestamp("2024-01-15")
        overrides = {"2024-01-15||-50.0||SomethingElse": "Shopping"}
        assert categorize(row, RULES, overrides) == "Dagligvarer"

    def test_andet_keyword_list_never_matches(self):
        # CATEGORIES["Andet"] is empty — ensure we never return early from it
        row = _row(navn="Spotify")
        assert categorize(row, RULES) == "Abonnementer"


# ── _normalise ────────────────────────────────────────────────────────────────

def _minimal_raw_df(**extra_cols):
    """Build the smallest DataFrame that _normalise accepts."""
    data = {
        "Bogføringsdato": ["15-01-2024", "16-01-2024"],
        "Beløb":          ["-100,00",   "5.000,00"],
        "Navn":           ["Netto",     "Løn"],
        "Beskrivelse":    ["Dagkøb",    "Januar løn"],
        "Modtager":       [None,        None],
        "Afsender":       [None,        None],
        "Valuta":         ["DKK",       "DKK"],
    }
    data.update(extra_cols)
    return pd.DataFrame(data)


class TestNormalise:
    def test_happy_path_returns_dataframe(self):
        df = _normalise(_minimal_raw_df())
        assert df is not None
        assert isinstance(df, pd.DataFrame)

    def test_columns_renamed_to_canonical(self):
        df = _normalise(_minimal_raw_df())
        assert "dato" in df.columns
        assert "beløb" in df.columns

    def test_amounts_parsed_correctly(self):
        df = _normalise(_minimal_raw_df())
        amounts = set(df["beløb"].tolist())
        assert -100.0 in amounts
        assert 5000.0 in amounts

    def test_dates_parsed_as_timestamps(self):
        df = _normalise(_minimal_raw_df())
        real = df[~df["reserveret"]]
        assert pd.api.types.is_datetime64_any_dtype(real["dato"])

    def test_type_column_assigned(self):
        df = _normalise(_minimal_raw_df())
        assert set(df["type"].unique()).issubset({"Indkomst", "Udgift"})

    def test_kategori_column_assigned(self):
        df = _normalise(_minimal_raw_df())
        assert "kategori" in df.columns

    def test_missing_dato_column_returns_none(self):
        raw = pd.DataFrame({"Beløb": ["-100,00"]})
        assert _normalise(raw) is None

    def test_missing_belob_column_returns_none(self):
        raw = pd.DataFrame({"Bogføringsdato": ["15-01-2024"]})
        assert _normalise(raw) is None

    def test_reserveret_rows_tagged(self):
        raw = _minimal_raw_df()
        raw.loc[len(raw)] = {
            "Bogføringsdato": "Reserveret",
            "Beløb": "-50,00",
            "Navn": "Pending",
            "Beskrivelse": "",
            "Modtager": None,
            "Afsender": None,
            "Valuta": "DKK",
        }
        df = _normalise(raw)
        assert df["reserveret"].any()

    def test_sorted_descending_by_dato(self):
        df = _normalise(_minimal_raw_df())
        real = df[~df["reserveret"]].reset_index(drop=True)
        dates = real["dato"].dropna().tolist()
        assert dates == sorted(dates, reverse=True)


# ── parse_csv_file ────────────────────────────────────────────────────────────

MINIMAL_CSV = textwrap.dedent("""\
    Bogføringsdato;Beløb;Afsender;Modtager;Navn;Beskrivelse;Saldo;Valuta
    15-01-2024;-100,00;;Netto;Netto Amager;Dagkøb;10.000,00;DKK
    14-01-2024;5.000,00;;Nikolas;Løn;Januar løn;10.100,00;DKK
""")


class TestParseCsvFile:
    def test_accepts_path(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_bytes(MINIMAL_CSV.encode("utf-8"))
        df = parse_csv_file(csv_file)
        assert df is not None
        assert len(df) == 2

    def test_accepts_file_like_object(self):
        buf = io.BytesIO(MINIMAL_CSV.encode("utf-8"))
        df = parse_csv_file(buf)
        assert df is not None
        assert len(df) == 2

    def test_amounts_parsed(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_bytes(MINIMAL_CSV.encode("utf-8"))
        df = parse_csv_file(csv_file)
        amounts = set(df["beløb"].tolist())
        assert -100.0 in amounts
        assert 5000.0 in amounts

    def test_kategori_assigned(self, tmp_path):
        csv_file = tmp_path / "test.csv"
        csv_file.write_bytes(MINIMAL_CSV.encode("utf-8"))
        df = parse_csv_file(csv_file)
        assert "kategori" in df.columns
        dagligvarer_row = df[df["beløb"] == -100.0]
        assert not dagligvarer_row.empty
        assert dagligvarer_row.iloc[0]["kategori"] == "Dagligvarer"

    def test_latin1_encoding(self, tmp_path):
        csv_file = tmp_path / "test_latin1.csv"
        csv_file.write_bytes(MINIMAL_CSV.encode("latin-1"))
        df = parse_csv_file(csv_file)
        assert df is not None

    def test_empty_file_returns_none(self, tmp_path):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_bytes(b"")
        # Empty CSV — pandas will raise or return empty; _normalise returns None
        df = parse_csv_file(csv_file)
        assert df is None or len(df) == 0
