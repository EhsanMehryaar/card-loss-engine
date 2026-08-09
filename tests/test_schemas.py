from pyspark.sql.types import DateType, DoubleType, IntegerType, StringType

from src.ingest.schemas import acquisition_schema, performance_schema


def test_acquisition_schema_is_explicit_and_stable() -> None:
    fields = {field.name: field.dataType for field in acquisition_schema().fields}

    assert isinstance(fields["loan_id"], StringType)
    assert isinstance(fields["origination_month"], DateType)
    assert isinstance(fields["original_upb"], DoubleType)
    assert isinstance(fields["orig_score"], IntegerType)
    assert "property_state" in fields
    assert "censoring_date" not in fields


def test_performance_schema_is_explicit_and_stable() -> None:
    fields = {field.name: field.dataType for field in performance_schema().fields}

    assert isinstance(fields["loan_id"], StringType)
    assert isinstance(fields["as_of_month"], DateType)
    assert isinstance(fields["current_upb"], DoubleType)
    assert isinstance(fields["delinquency_status"], IntegerType)
