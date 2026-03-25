"""
Tests for the tree-sitter multi-language parser.

Covers: TypeScript, Go, Rust parsing including symbols, imports,
docstrings, visibility, and get_symbol_source.
"""
import os
import pytest
import sys

# Ensure indexer is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indexer.treesitter_parser import (
    parse_file,
    get_language,
    get_symbol_source,
    ParsedFile,
    ParsedSymbol,
    ParsedImport,
    EXTENSION_MAP,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# Extension mapping
# ---------------------------------------------------------------------------

class TestGetLanguage:
    def test_typescript(self):
        assert get_language(".ts") == "typescript"

    def test_tsx(self):
        assert get_language(".tsx") == "tsx"

    def test_go(self):
        assert get_language(".go") == "go"

    def test_rust(self):
        assert get_language(".rs") == "rust"

    def test_python_unsupported(self):
        assert get_language(".py") is None

    def test_unknown(self):
        assert get_language(".xyzabc") is None


# ---------------------------------------------------------------------------
# TypeScript parsing
# ---------------------------------------------------------------------------

class TestTypeScriptParsing:
    @pytest.fixture
    def parsed(self) -> ParsedFile:
        return parse_file(os.path.join(FIXTURES_DIR, "sample.ts"), "typescript")

    def test_module_docstring(self, parsed: ParsedFile):
        assert parsed.module_docstring is not None
        assert "Sample TypeScript module" in parsed.module_docstring or parsed.module_docstring.startswith("/**")

    def test_symbol_count(self, parsed: ParsedFile):
        # greet, _privateHelper, calculateScore, UserService, AppConfig
        assert len(parsed.symbols) == 8

    def test_function_greet(self, parsed: ParsedFile):
        greet = next(s for s in parsed.symbols if s.name == "greet")
        assert greet.kind == "function"
        assert greet.is_public is True
        assert greet.docstring is not None
        assert "Greeting helper" in greet.docstring

    def test_private_function(self, parsed: ParsedFile):
        helper = next(s for s in parsed.symbols if s.name == "_privateHelper")
        assert helper.is_public is False

    def test_class_with_methods(self, parsed: ParsedFile):
        svc = next(s for s in parsed.symbols if s.name == "UserService")
        assert svc.kind == "class"
        assert "getName" in svc.methods
        assert "updateEmail" in svc.methods
        assert svc.docstring is not None
        assert "user in the system" in svc.docstring

    def test_interface(self, parsed: ParsedFile):
        config = next(s for s in parsed.symbols if s.name == "AppConfig")
        assert config.kind == "interface"
        assert config.docstring is not None
        assert "Configuration options" in config.docstring

    def test_imports(self, parsed: ParsedFile):
        modules = [i.module for i in parsed.imports]
        assert "events" in modules
        assert "./config" in modules

    def test_line_numbers(self, parsed: ParsedFile):
        greet = next(s for s in parsed.symbols if s.name == "greet")
        assert greet.start_line == 10
        assert greet.end_line == 12


# ---------------------------------------------------------------------------
# Go parsing
# ---------------------------------------------------------------------------

class TestGoParsing:
    @pytest.fixture
    def parsed(self) -> ParsedFile:
        return parse_file(os.path.join(FIXTURES_DIR, "sample.go"), "go")

    def test_module_docstring(self, parsed: ParsedFile):
        assert parsed.module_docstring is not None
        assert "Package sample" in parsed.module_docstring

    def test_exported_function(self, parsed: ParsedFile):
        greet = next(s for s in parsed.symbols if s.name == "Greet")
        assert greet.kind == "function"
        assert greet.is_public is True
        assert greet.docstring is not None
        assert "greeting message" in greet.docstring

    def test_unexported_function(self, parsed: ParsedFile):
        helper = next(s for s in parsed.symbols if s.name == "helper")
        assert helper.is_public is False

    def test_method(self, parsed: ParsedFile):
        get_name = next(s for s in parsed.symbols if s.name == "GetName")
        assert get_name.kind == "method"
        assert get_name.is_public is True
        assert "display name" in (get_name.docstring or "")

    def test_unexported_method(self, parsed: ParsedFile):
        m = next(s for s in parsed.symbols if s.name == "unexportedMethod")
        assert m.is_public is False

    def test_struct(self, parsed: ParsedFile):
        svc = next(s for s in parsed.symbols if s.name == "UserService")
        assert svc.kind == "struct"
        assert svc.is_public is True

    def test_interface(self, parsed: ParsedFile):
        handler = next(s for s in parsed.symbols if s.name == "Handler")
        assert handler.kind == "interface"

    def test_imports(self, parsed: ParsedFile):
        modules = [i.module for i in parsed.imports]
        assert "fmt" in modules
        assert "net/http" in modules


# ---------------------------------------------------------------------------
# Rust parsing
# ---------------------------------------------------------------------------

class TestRustParsing:
    @pytest.fixture
    def parsed(self) -> ParsedFile:
        return parse_file(os.path.join(FIXTURES_DIR, "sample.rs"), "rust")

    def test_module_docstring(self, parsed: ParsedFile):
        assert parsed.module_docstring is not None
        assert "Sample Rust module" in parsed.module_docstring

    def test_pub_function(self, parsed: ParsedFile):
        greet = next(s for s in parsed.symbols if s.name == "greet")
        assert greet.kind == "function"
        assert greet.is_public is True
        assert greet.docstring is not None
        assert "Greets a user" in greet.docstring

    def test_private_function(self, parsed: ParsedFile):
        helper = next(s for s in parsed.symbols if s.name == "_private_helper")
        assert helper.is_public is False

    def test_struct(self, parsed: ParsedFile):
        user = next(s for s in parsed.symbols if s.name == "User")
        assert user.kind == "struct"
        assert user.is_public is True
        assert "user in the system" in (user.docstring or "")

    def test_enum(self, parsed: ParsedFile):
        role = next(s for s in parsed.symbols if s.name == "Role")
        assert role.kind == "enum"
        assert role.is_public is True

    def test_trait(self, parsed: ParsedFile):
        disp = next(s for s in parsed.symbols if s.name == "Displayable")
        assert disp.kind == "trait"
        assert disp.is_public is True

    def test_no_impl_functions_as_standalone(self, parsed: ParsedFile):
        """Functions inside impl blocks should not appear as standalone functions."""
        names = [s.name for s in parsed.symbols if s.kind == "function"]
        assert "display" not in names

    def test_imports(self, parsed: ParsedFile):
        modules = [i.module for i in parsed.imports]
        assert "std::collections::HashMap" in modules
        assert "std::fmt" in modules


# ---------------------------------------------------------------------------
# get_symbol_source
# ---------------------------------------------------------------------------

class TestGetSymbolSource:
    def test_found(self):
        result = get_symbol_source(
            os.path.join(FIXTURES_DIR, "sample.go"), "Greet"
        )
        assert result["found"] is True
        assert result["kind"] == "function"
        assert "fmt.Sprintf" in result["source"]

    def test_not_found(self):
        result = get_symbol_source(
            os.path.join(FIXTURES_DIR, "sample.go"), "NonExistent"
        )
        assert result["found"] is False
        assert "available_symbols" in result

    def test_file_not_found(self):
        result = get_symbol_source("/no/such/file.ts", "foo")
        assert result["found"] is False
        assert "not found" in result["error"].lower()

    def test_unsupported_extension(self):
        result = get_symbol_source("/some/file.java", "foo")
        assert result["found"] is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            parse_file("/no/such/file.ts", "typescript")

    def test_unsupported_language(self):
        with pytest.raises(ValueError):
            parse_file(os.path.join(FIXTURES_DIR, "sample.ts"), "java")
