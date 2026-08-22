from __future__ import annotations

from backend.services.table_analysis_files import (
    build_table_analysis_metadata,
    is_table_analysis_file,
)
from backend.services.minio_paths import (
    normalize_minio_endpoint,
    parse_minio_source,
)


def test_is_table_analysis_file_by_extension_and_mime() -> None:
    assert is_table_analysis_file("data.xlsx")
    assert is_table_analysis_file("data.csv")
    assert is_table_analysis_file("unknown.bin", "text/csv")
    assert not is_table_analysis_file("paper.pdf", "application/pdf")


def test_build_table_analysis_metadata_for_table_file() -> None:
    meta = build_table_analysis_metadata(
        file_id="uf_123",
        file_name="data.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        storage_bucket="bucket",
        storage_object="users/u/thread/t/data.xlsx",
    )

    assert meta is not None
    assert meta["enabled"] is True
    assert meta["file_id"] == "uf_123"
    assert meta["storage_object"] == "users/u/thread/t/data.xlsx"


def test_build_table_analysis_metadata_skips_non_table_file() -> None:
    assert build_table_analysis_metadata(
        file_id="uf_pdf",
        file_name="paper.pdf",
        mime_type="application/pdf",
    ) is None


def test_excel_engine_normalizes_minio_endpoint_with_public_path() -> None:
    assert normalize_minio_endpoint("http://127.0.0.1:9000/files") == "127.0.0.1:9000"
    assert normalize_minio_endpoint("minio.example.com:9000/files") == "minio.example.com:9000"


def test_excel_engine_parses_bucket_qualified_minio_source() -> None:
    bucket, object_name = parse_minio_source("minio://user-files/users/u/file.xlsx")

    assert bucket == "user-files"
    assert object_name == "users/u/file.xlsx"
