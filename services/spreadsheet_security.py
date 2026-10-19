"""Keep registration text literal at spreadsheet export boundaries."""


def literal_workbook(workbook):
    """Exports contain data, not formulas; preserve strings without evaluation."""
    for worksheet in workbook.worksheets:
        for row in worksheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
