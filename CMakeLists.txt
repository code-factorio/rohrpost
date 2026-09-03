# Rohrpost native build: the `rp` binary in C++23.
#
# The Python package under src/rohrpost is the frozen reference implementation;
# this tree is the shipped tool. Everything here builds offline: the three
# header-only dependencies are vendored under third_party/, and the only
# system library is the platform HTTP stack (libcurl on POSIX, WinHTTP on
# Windows) used by the GitHub REST fallback and `rp doctor`.
cmake_minimum_required(VERSION 3.25)

project(rohrpost
  VERSION 0.1.0
  DESCRIPTION "Git-native ticket system for agentic coding workflows"
  LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(RP_BUILD_TESTS "Build the doctest unit tests" ON)
option(RP_STATIC_RUNTIME "Link the C++ runtime statically (release binaries)" OFF)
option(RP_WERROR "Treat compiler warnings as errors" OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

include(cmake/Warnings.cmake)
include(cmake/Runtime.cmake)

# --- third-party (vendored, header-only) -------------------------------------
add_library(rp_third_party INTERFACE)
target_include_directories(rp_third_party SYSTEM INTERFACE
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
# toml++ and nlohmann/json are used header-only; doctest only in tests.
target_compile_definitions(rp_third_party INTERFACE
  TOML_EXCEPTIONS=0
  JSON_USE_IMPLICIT_CONVERSIONS=0)

# --- the core library (everything except main) --------------------------------
set(RP_CORE_SOURCES
  cpp/src/api.cpp
  cpp/src/argparse.cpp
  cpp/src/cli.cpp
  cpp/src/compact.cpp
  cpp/src/config.cpp
  cpp/src/doctor.cpp
  cpp/src/entropy.cpp
  cpp/src/events.cpp
  cpp/src/fold.cpp
  cpp/src/ids.cpp
  cpp/src/io.cpp
  cpp/src/json.cpp
  cpp/src/merge.cpp
  cpp/src/paths.cpp
  cpp/src/pyfmt.cpp
  cpp/src/shadow.cpp
  cpp/src/stats.cpp
  cpp/src/store.cpp
  cpp/src/subprocess.cpp
  cpp/src/sync.cpp
  cpp/src/timeutil.cpp
  cpp/src/util.cpp
  cpp/src/providers/github.cpp)

if(WIN32)
  list(APPEND RP_CORE_SOURCES cpp/src/http_winhttp.cpp)
else()
  list(APPEND RP_CORE_SOURCES cpp/src/http_curl.cpp)
endif()

add_library(rp_core STATIC ${RP_CORE_SOURCES})
target_include_directories(rp_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/cpp/include)
target_link_libraries(rp_core PUBLIC rp_third_party)
target_compile_definitions(rp_core PUBLIC RP_VERSION="${PROJECT_VERSION}")
rp_apply_warnings(rp_core)
rp_apply_runtime(rp_core)

if(WIN32)
  target_compile_definitions(rp_core PUBLIC
    NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS UNICODE _UNICODE)
  target_link_libraries(rp_core PUBLIC winhttp bcrypt)
else()
  find_package(CURL REQUIRED)
  target_link_libraries(rp_core PUBLIC CURL::libcurl)
  find_package(Threads REQUIRED)
  target_link_libraries(rp_core PUBLIC Threads::Threads)
endif()

# --- the binary ---------------------------------------------------------------
add_executable(rp cpp/src/main.cpp)
target_link_libraries(rp PRIVATE rp_core)
rp_apply_warnings(rp)
rp_apply_runtime(rp)

install(TARGETS rp RUNTIME DESTINATION bin)

# --- tests --------------------------------------------------------------------
if(RP_BUILD_TESTS)
  enable_testing()
  file(GLOB RP_TEST_SOURCES CONFIGURE_DEPENDS cpp/tests/*.cpp)
  add_executable(rp_tests ${RP_TEST_SOURCES})
  target_link_libraries(rp_tests PRIVATE rp_core)
  rp_apply_warnings(rp_tests)
  rp_apply_runtime(rp_tests)
  add_test(NAME rp_tests COMMAND rp_tests)
endif()
