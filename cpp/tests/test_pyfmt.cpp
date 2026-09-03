// Python-compat primitives: the byte-level rules the log and --json depend on.
#include "rohrpost/pyfmt.hpp"

#include <doctest/doctest.h>

#include <ostream>
#include <string>

using namespace rp;

TEST_CASE("float_repr follows Python's repr layout") {
    CHECK(py::float_repr(0.0) == "0.0");
    CHECK(py::float_repr(-0.0) == "-0.0");
    CHECK(py::float_repr(1.0) == "1.0");
    CHECK(py::float_repr(12.5) == "12.5");
    CHECK(py::float_repr(0.001) == "0.001");
    CHECK(py::float_repr(100.0) == "100.0");
    CHECK(py::float_repr(1234.5678) == "1234.5678");
    CHECK(py::float_repr(1e16) == "1e+16");
    CHECK(py::float_repr(1e15) == "1000000000000000.0");
    CHECK(py::float_repr(1e-5) == "1e-05");
    CHECK(py::float_repr(0.0001) == "0.0001");
    CHECK(py::float_repr(2.5e-7) == "2.5e-07");
    CHECK(py::float_repr(1e22) == "1e+22");
    CHECK(py::float_repr(123456789012345678.0) == "1.2345678901234568e+17");
    CHECK(py::float_repr(0.1 + 0.2) == "0.30000000000000004");
    CHECK(py::float_repr(1.0 / 3.0) == "0.3333333333333333");
    CHECK(py::float_repr(5e-324) == "5e-324");
}

TEST_CASE("round follows Python: half to even, correctly rounded digits") {
    CHECK(py::round_half_even(2.5) == 2);
    CHECK(py::round_half_even(3.5) == 4);
    CHECK(py::round_half_even(0.5) == 0);
    CHECK(py::round_half_even(-0.5) == 0);
    CHECK(py::round_digits(1.2345, 3) == 1.234);
    CHECK(py::round_digits(2.675, 2) == 2.67);
    CHECK(py::float_repr(py::round_digits(100.0 * 3 / 7, 2)) == "42.86");
    CHECK(py::float_repr(py::round_digits(0.125, 2)) == "0.12");
    CHECK(py::float_repr(py::round_digits(0.375, 2)) == "0.38");
}

TEST_CASE("repr picks quotes and escapes like Python") {
    CHECK(py::repr("a") == "'a'");
    CHECK(py::repr("it's") == "\"it's\"");
    CHECK(py::repr("say \"hi\"") == "'say \"hi\"'");
    CHECK(py::repr("both ' and \"") == "'both \\' and \"'");
    CHECK(py::repr("\n\t\\") == "'\\n\\t\\\\'");
    CHECK(py::repr(std::string("\x00\x7f", 2)) == "'\\x00\\x7f'");
    CHECK(py::repr("\xc2\x80\xc2\xa0\xc2\xad\xe2\x80\x8b\xe2\x80\xa8\xe3\x80\x80") == "'\\x80\\xa0\\xad\\u200b\\u2028\\u3000'");
    CHECK(py::repr("\xc3\xa9\xf0\x9f\x8e\x89") == "'\xc3\xa9\xf0\x9f\x8e\x89'");
    CHECK(py::repr("\x1b[0m") == "'\\x1b[0m'");
}

TEST_CASE("strip removes Python's whitespace set") {
    CHECK(py::strip("  a b  ") == "a b");
    CHECK(py::strip("\t\n\r\x0b\x0c\x1c\x1d\x1e\x1fx") == "x");
    CHECK(py::strip("\xc2\xa0x\xe3\x80\x80") == "x");
    CHECK(py::strip("\xc2\x85x\xe2\x80\xa8") == "x");
    CHECK(py::strip("\xef\xbb\xbfx") == "\xef\xbb\xbfx");  // BOM is not whitespace
    CHECK(py::strip("") == "");
    CHECK(py::strip("   ") == "");
}

TEST_CASE("parse_int follows int()") {
    CHECK(py::parse_int("3") == 3);
    CHECK(py::parse_int(" 3 ") == 3);
    CHECK(py::parse_int("+3") == 3);
    CHECK(py::parse_int("-1") == -1);
    CHECK(py::parse_int("1_0") == 10);
    CHECK(py::parse_int("010") == 10);
    CHECK(py::parse_int("-0") == 0);
    CHECK_FALSE(py::parse_int("1__0"));
    CHECK_FALSE(py::parse_int("_1"));
    CHECK_FALSE(py::parse_int("1_"));
    CHECK_FALSE(py::parse_int("3.0"));
    CHECK_FALSE(py::parse_int(""));
    CHECK_FALSE(py::parse_int("0x10"));
    CHECK_FALSE(py::parse_int("x"));
}

TEST_CASE("validate_utf8 reports Python's decode error shape") {
    CHECK_FALSE(py::validate_utf8("caf\xc3\xa9 \xf0\x9f\x8e\x89"));
    auto err = py::validate_utf8("caf\xe9");
    REQUIRE(err);
    CHECK(err->message("caf\xe9") == "'utf-8' codec can't decode byte 0xe9 in position 3: unexpected end of data");
    err = py::validate_utf8("\xf0\x9f\x8e");
    REQUIRE(err);
    CHECK(err->message("\xf0\x9f\x8e") == "'utf-8' codec can't decode bytes in position 0-2: unexpected end of data");
    err = py::validate_utf8("\x80");
    REQUIRE(err);
    CHECK(err->message("\x80") == "'utf-8' codec can't decode byte 0x80 in position 0: invalid start byte");
    err = py::validate_utf8("\xe0\x80");
    REQUIRE(err);
    CHECK(err->message("\xe0\x80") == "'utf-8' codec can't decode byte 0xe0 in position 0: invalid continuation byte");
}

TEST_CASE("split_lines uses universal newlines") {
    const auto lines = py::split_lines("a\r\nb\rc\n\nd");
    REQUIRE(lines.size() == 5);
    CHECK(lines[0] == "a");
    CHECK(lines[1] == "b");
    CHECK(lines[2] == "c");
    CHECK(lines[3] == "");
    CHECK(lines[4] == "d");
    CHECK(py::split_lines("a\n").size() == 1);
    CHECK(py::split_lines("").empty());
}
