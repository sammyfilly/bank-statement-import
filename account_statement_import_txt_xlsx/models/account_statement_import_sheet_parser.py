# Copyright 2019 ForgeFlow, S.L.
# Copyright 2020 CorporateHub (https://corporatehub.eu)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import itertools
import logging
from datetime import datetime
from decimal import Decimal
from io import StringIO
from os import path

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from csv import reader

    import xlrd
    from xlrd.xldate import xldate_as_datetime
except (ImportError, IOError) as err:  # pragma: no cover
    _logger.error(err)

try:
    import chardet
except ImportError:
    _logger.warning(
        "chardet library not found, please install it "
        "from http://pypi.python.org/pypi/chardet"
    )


class AccountStatementImportSheetParser(models.TransientModel):
    _name = "account.statement.import.sheet.parser"
    _description = "Bank Statement Import Sheet Parser"

    @api.model
    def parse_header(self, data_file, encoding, csv_options):
        try:
            workbook = xlrd.open_workbook(
                file_contents=data_file,
                encoding_override=encoding if encoding else None,
            )
            sheet = workbook.sheet_by_index(0)
            values = sheet.row_values(0)
            return [str(value) for value in values]
        except xlrd.XLRDError:
            pass

        data = StringIO(data_file.decode(encoding or "utf-8"))
        csv_data = reader(data, **csv_options)
        return list(next(csv_data))

    @api.model
    def parse(self, data_file, mapping, filename):
        journal = self.env["account.journal"].browse(self.env.context.get("journal_id"))
        currency_code = (journal.currency_id or journal.company_id.currency_id).name
        account_number = journal.bank_account_id.acc_number

        lines = self._parse_lines(mapping, data_file, currency_code)
        if not lines:
            return currency_code, account_number, [{"transactions": []}]

        lines = list(sorted(lines, key=lambda line: line["timestamp"]))
        first_line = lines[0]
        data = {
            "date": first_line["timestamp"].date(),
            "name": _("%s: %s")
            % (
                journal.code,
                path.basename(filename),
            ),
        }

        if mapping.balance_column:
            balance_start = first_line["balance"]
            balance_start -= first_line["amount"]
            last_line = lines[-1]
            balance_end = last_line["balance"]
            data |= {
                "balance_start": float(balance_start),
                "balance_end_real": float(balance_end),
            }
        transactions = list(
            itertools.chain.from_iterable(
                map(lambda line: self._convert_line_to_transactions(line), lines)
            )
        )
        data["transactions"] = transactions

        return currency_code, account_number, [data]

    def _get_column_indexes(self, header, column_name, mapping):
        column_indexes = []
        if mapping[column_name] and "," in mapping[column_name]:
            # We have to concatenate the values
            column_names_or_indexes = mapping[column_name].split(",")
        else:
            column_names_or_indexes = [mapping[column_name]]
        for column_name_or_index in column_names_or_indexes:
            if not column_name_or_index:
                continue
            column_index = None
            if mapping.no_header:
                try:
                    column_index = int(column_name_or_index)
                except Exception:
                    pass
                if column_index is not None:
                    column_indexes.append(column_index)
            else:
                column_indexes.append(header.index(column_name_or_index))
        return column_indexes

    def _get_column_names(self):
        return [
            "timestamp_column",
            "currency_column",
            "amount_column",
            "amount_debit_column",
            "amount_credit_column",
            "balance_column",
            "original_currency_column",
            "original_amount_column",
            "debit_credit_column",
            "transaction_id_column",
            "description_column",
            "notes_column",
            "reference_column",
            "partner_name_column",
            "bank_name_column",
            "bank_account_column",
        ]

    def _parse_lines(self, mapping, data_file, currency_code):
        try:
            workbook = xlrd.open_workbook(
                file_contents=data_file,
                encoding_override=(
                    mapping.file_encoding if mapping.file_encoding else None
                ),
            )
            csv_or_xlsx = (
                workbook,
                workbook.sheet_by_index(0),
            )
        except xlrd.XLRDError:
            csv_options = {}
            if csv_delimiter := mapping._get_column_delimiter_character():
                csv_options["delimiter"] = csv_delimiter
            if mapping.quotechar:
                csv_options["quotechar"] = mapping.quotechar
            try:
                decoded_file = data_file.decode(mapping.file_encoding or "utf-8")
            except UnicodeDecodeError:
                if detected_encoding := chardet.detect(data_file).get(
                    "encoding", False
                ):
                    decoded_file = data_file.decode(detected_encoding)
                else:
                    raise UserError(
                        _("No valid encoding was found for the attached file")
                    )
            csv_or_xlsx = reader(StringIO(decoded_file), **csv_options)
        header = False
        if not mapping.no_header:
            if isinstance(csv_or_xlsx, tuple):
                header = [str(value) for value in csv_or_xlsx[1].row_values(0)]
            else:
                header = [value.strip() for value in next(csv_or_xlsx)]
        columns = {
            column_name: self._get_column_indexes(header, column_name, mapping)
            for column_name in self._get_column_names()
        }
        return self._parse_rows(mapping, currency_code, csv_or_xlsx, columns)

    def _get_values_from_column(self, values, columns, column_name):
        indexes = columns[column_name]
        max_index = len(values) - 1
        content_l = [
            values[index]
            for index in indexes
            if isinstance(index, int)
            and index <= max_index
            or not isinstance(index, int)
            and index in values
        ]
        if all(isinstance(content, str) for content in content_l):
            return " ".join(content_l)
        return content_l[0]

    def _parse_row(self, mapping, currency_code, values, columns):  # noqa: C901
        timestamp = self._get_values_from_column(values, columns, "timestamp_column")
        currency = (
            self._get_values_from_column(values, columns, "currency_column")
            if columns["currency_column"]
            else currency_code
        )

        def _decimal(column_name):
            if columns[column_name]:
                return self._parse_decimal(
                    self._get_values_from_column(values, columns, column_name),
                    mapping,
                )

        amount = _decimal("amount_column")
        if not amount:
            amount = abs(_decimal("amount_debit_column") or 0)
        if not amount:
            amount = -abs(_decimal("amount_credit_column") or 0)

        balance = (
            self._get_values_from_column(values, columns, "balance_column")
            if columns["balance_column"]
            else None
        )
        original_currency = (
            self._get_values_from_column(values, columns, "original_currency_column")
            if columns["original_currency_column"]
            else None
        )
        original_amount = (
            self._get_values_from_column(values, columns, "original_amount_column")
            if columns["original_amount_column"]
            else None
        )
        debit_credit = (
            self._get_values_from_column(values, columns, "debit_credit_column")
            if columns["debit_credit_column"]
            else None
        )
        transaction_id = (
            self._get_values_from_column(values, columns, "transaction_id_column")
            if columns["transaction_id_column"]
            else None
        )
        description = (
            self._get_values_from_column(values, columns, "description_column")
            if columns["description_column"]
            else None
        )
        notes = (
            self._get_values_from_column(values, columns, "notes_column")
            if columns["notes_column"]
            else None
        )
        reference = (
            self._get_values_from_column(values, columns, "reference_column")
            if columns["reference_column"]
            else None
        )
        partner_name = (
            self._get_values_from_column(values, columns, "partner_name_column")
            if columns["partner_name_column"]
            else None
        )
        bank_name = (
            self._get_values_from_column(values, columns, "bank_name_column")
            if columns["bank_name_column"]
            else None
        )
        bank_account = (
            self._get_values_from_column(values, columns, "bank_account_column")
            if columns["bank_account_column"]
            else None
        )

        if currency != currency_code:
            return {}

        if isinstance(timestamp, str):
            timestamp = datetime.strptime(timestamp, mapping.timestamp_format)

        balance = self._parse_decimal(balance, mapping) if balance else None
        if debit_credit:
            amount = amount.copy_abs()
            if debit_credit == mapping.debit_value:
                amount = -amount

        if original_amount:
            original_amount = self._parse_decimal(original_amount, mapping).copy_sign(
                amount
            )
        else:
            original_amount = 0.0

        line = {
            "timestamp": timestamp,
            "amount": amount,
            "currency": currency,
            "original_amount": original_amount,
            "original_currency": original_currency,
        }
        if balance is not None:
            line["balance"] = balance
        if transaction_id is not None:
            line["transaction_id"] = transaction_id
        if description is not None:
            line["description"] = description
        if notes is not None:
            line["notes"] = notes
        if reference is not None:
            line["reference"] = reference
        if partner_name is not None:
            line["partner_name"] = partner_name
        if bank_name is not None:
            line["bank_name"] = bank_name
        if bank_account is not None:
            line["bank_account"] = bank_account
        return line

    def _parse_rows(self, mapping, currency_code, csv_or_xlsx, columns):  # noqa: C901
        if isinstance(csv_or_xlsx, tuple):
            rows = range(1, csv_or_xlsx[1].nrows)
        else:
            rows = csv_or_xlsx

        lines = []
        for row in rows:
            if isinstance(csv_or_xlsx, tuple):
                book = csv_or_xlsx[0]
                sheet = csv_or_xlsx[1]
                values = []
                for col_index in range(sheet.row_len(row)):
                    cell_type = sheet.cell_type(row, col_index)
                    cell_value = sheet.cell_value(row, col_index)
                    if cell_type == xlrd.XL_CELL_DATE:
                        cell_value = xldate_as_datetime(cell_value, book.datemode)
                    values.append(cell_value)
            else:
                values = list(row)
            if line := self._parse_row(mapping, currency_code, values, columns):
                lines.append(line)
        return lines

    @api.model
    def _convert_line_to_transactions(self, line):    # noqa: C901
        """Hook for extension"""
        timestamp = line["timestamp"]
        amount = line["amount"]
        currency = line["currency"]
        original_amount = line["original_amount"]
        original_currency = line["original_currency"]
        transaction_id = line.get("transaction_id")
        description = line.get("description")
        notes = line.get("notes")
        reference = line.get("reference")
        partner_name = line.get("partner_name")
        bank_name = line.get("bank_name")
        bank_account = line.get("bank_account")

        transaction = {
            "date": timestamp,
            "amount": str(amount),
        }

        if original_currency == currency:
            original_currency = None
            if not amount:
                amount = original_amount
            original_amount = "0.0"

        if original_currency:
            if original_currency := self.env["res.currency"].search(
                [("name", "=", original_currency)],
                limit=1,
            ):
                transaction["foreign_currency_id"] = original_currency.id
            if original_amount:
                transaction["amount_currency"] = str(original_amount)

        if currency:
            currency = self.env["res.currency"].search(
                [("name", "=", currency)],
                limit=1,
            )
        if currency:
            transaction["currency_id"] = currency.id

        if transaction_id:
            transaction[
                "unique_import_id"
            ] = f"{transaction_id}-{int(timestamp.timestamp())}"

        transaction["payment_ref"] = description or _("N/A")
        if reference:
            transaction["ref"] = reference

        note = ""
        if bank_name:
            note += _("Bank: %s; ") % (bank_name,)
        if bank_account:
            note += _("Account: %s; ") % (bank_account,)
        if transaction_id:
            note += _("Transaction ID: %s; ") % (transaction_id,)
        if note and notes:
            note = f"{notes}\n{note.strip()}"
        elif note:
            note = note.strip()
        elif notes:
            note = notes
        if note:
            transaction["narration"] = note

        if partner_name:
            transaction["partner_name"] = partner_name
        if bank_account:
            transaction["account_number"] = bank_account

        return [transaction]

    @api.model
    def _parse_decimal(self, value, mapping):
        if isinstance(value, Decimal):
            return value
        elif isinstance(value, float):
            return Decimal(value)
        thousands, decimal = mapping._get_float_separators()
        value = value.replace(thousands, "")
        value = value.replace(decimal, ".")
        return Decimal(value)
