from io import BytesIO

from openpyxl import Workbook

from app.domain.catalogue_ingestion import parse_catalogue_spreadsheet


def test_csv_parser_normalizes_aliases_and_records_invalid_rows() -> None:
    content = (
        "Product Name,Category,Fabric,Price,MOQ,Stock Quantity\n"
        "Cotton suit,Ladies Suits,cotton,₹550,50,120\n"
        "Missing category,,rayon,500,20,\n"
    ).encode()

    result = parse_catalogue_spreadsheet("surat.csv", content)

    assert result.total_rows == 2
    assert len(result.rows) == 1
    assert result.rows[0].title == "Cotton suit"
    assert result.rows[0].category.value == "ladies_suit"
    assert result.rows[0].price == 550
    assert result.rows[0].available_quantity == 120
    assert len(result.errors) == 1
    assert result.errors[0].row_number == 3


def test_xlsx_parser_uses_the_first_worksheet() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["title", "category", "fabric", "price", "currency", "moq"])
    worksheet.append(["Rayon suit", "salwar suit", "rayon", 480, "INR", 30])
    content = BytesIO()
    workbook.save(content)

    result = parse_catalogue_spreadsheet("ahmedabad.xlsx", content.getvalue())

    assert result.total_rows == 1
    assert result.rows[0].category.value == "salwar_suit"
    assert result.rows[0].price == 480