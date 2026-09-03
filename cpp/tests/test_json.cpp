// JSON serialisation must match msgspec (log lines) and json.dumps (--json).
#include "rohrpost/json.hpp"

#include <doctest/doctest.h>

using namespace rp;

TEST_CASE("compact style matches msgspec's encoder") {
    Json obj = Json::object();
    obj["title"] = std::string("a\"b\\c/d<e>\x00\x01\x1f\x7f\xc3\xa9  \xf0\x9f\x8e\x89\n\r\t\b\f\xe2\x80\xa8", 30);
    obj["priority"] = 2;
    obj["f"] = 1.5;
    obj["n"] = nullptr;
    obj["b"] = true;
    obj["l"] = Json::array({1, "x"});
    obj["d"] = Json::object({{"k", "v"}});
    CHECK(json::dumps(obj, json::kCompact) ==
          "{\"title\":\"a\\\"b\\\\c/d<e>\\u0000\\u0001\\u001f\x7f\xc3\xa9  \xf0\x9f\x8e\x89\\n\\r\\t\\b\\f\xe2\x80\xa8\","
          "\"priority\":2,\"f\":1.5,\"n\":null,\"b\":true,\"l\":[1,\"x\"],\"d\":{\"k\":\"v\"}}");
}

TEST_CASE("default style matches json.dumps with ensure_ascii") {
    Json obj = Json::object();
    obj["s"] = std::string("\x7f\xc3\xa9\xf0\x9f\x8e\x89\n\xe2\x80\xa8");
    obj["e"] = Json::array();
    obj["o"] = Json::object();
    obj["f"] = 1.0;
    obj["h"] = 1e16;
    obj["i"] = 1e-5;
    CHECK(json::dumps(obj, json::kPyDefault) ==
          "{\"s\": \"\\u007f\\u00e9\\ud83c\\udf89\\n\\u2028\", \"e\": [], \"o\": {}, \"f\": 1.0, \"h\": 1e+16, \"i\": 1e-05}");
}

TEST_CASE("pretty style matches json.dump(indent=2, ensure_ascii=False)") {
    Json obj = Json::object();
    obj["s"] = "\xc3\xa9";
    obj["e"] = Json::array();
    obj["l"] = Json::array({1, Json::array({2, Json::object()})});
    obj["u"] = Json::object({{"a", Json::array()}, {"b", Json::object({{"c", 1}})}});
    CHECK(json::dumps(obj, json::kPretty) ==
          "{\n  \"s\": \"\xc3\xa9\",\n  \"e\": [],\n  \"l\": [\n    1,\n    [\n      2,\n      {}\n    ]\n  ],\n"
          "  \"u\": {\n    \"a\": [],\n    \"b\": {\n      \"c\": 1\n    }\n  }\n}");
}

TEST_CASE("sorted raw style matches the shadow writer") {
    Json obj = Json::object();
    obj["b"] = 1;
    obj["a"] = Json::array({3, Json::object({{"z", 1}, {"y", 2}})});
    CHECK(json::dumps(obj, json::kSortedRaw) == "{\"a\": [3, {\"y\": 2, \"z\": 1}], \"b\": 1}");
}

TEST_CASE("parse keeps last duplicate key at the first position and rejects BOM/NaN") {
    auto parsed = json::parse("{\"a\":1,\"b\":2,\"a\":3}");
    REQUIRE(parsed);
    CHECK(json::dumps(*parsed, json::kCompact) == "{\"a\":3,\"b\":2}");
    CHECK_FALSE(json::parse("\xef\xbb\xbf{}"));
    CHECK_FALSE(json::parse("{\"a\":NaN}"));
    CHECK_FALSE(json::parse("{} trailing"));
    auto floats = json::parse("{\"a\":1.0,\"b\":-0.0,\"c\":1e2,\"e\":0.1}");
    REQUIRE(floats);
    CHECK(json::dumps(*floats, json::kCompact) == "{\"a\":1.0,\"b\":-0.0,\"c\":100.0,\"e\":0.1}");
}

TEST_CASE("py_repr renders Python literals") {
    CHECK(json::py_repr(Json()) == "None");
    CHECK(json::py_repr(Json(true)) == "True");
    CHECK(json::py_repr(Json(2.5)) == "2.5");
    CHECK(json::py_repr(Json::array({1, "a", nullptr})) == "[1, 'a', None]");
    CHECK(json::py_repr(Json::object({{"k", "v"}, {"n", 1}})) == "{'k': 'v', 'n': 1}");
    CHECK(json::py_str(Json("plain")) == "plain");
}

TEST_CASE("py_equal coerces numbers like Python") {
    CHECK(json::py_equal(Json(1), Json(1.0)));
    CHECK(json::py_equal(Json(true), Json(1)));
    CHECK_FALSE(json::py_equal(Json("1"), Json(1)));
    CHECK(json::py_equal(Json::array({1, 2}), Json::array({1.0, 2})));
}
