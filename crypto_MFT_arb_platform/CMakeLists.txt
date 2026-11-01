cmake_minimum_required(VERSION 3.18)
project(hft_platform_kit_ems LANGUAGES CXX)

set(PYBIND11_FINDPYTHON ON)
find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(_ems MODULE cpp/ems.cpp)
target_compile_features(_ems PRIVATE cxx_std_20)
target_compile_definitions(_ems PRIVATE PYBIND11_DETAILED_ERROR_MESSAGES)

if(MSVC)
  target_compile_options(_ems PRIVATE /W4 /permissive-)
else()
  target_compile_options(_ems PRIVATE -Wall -Wextra -Wpedantic)
endif()

install(
  TARGETS _ems
  LIBRARY DESTINATION hft_platform_kit
  RUNTIME DESTINATION hft_platform_kit
)

# The sim_engine queue matching engine (GAP03). crypto_stat_arb.maker_engine loads it
# from crypto_stat_arb/sim_engine/; without it every wheel falls back to the Python mirror.
set(MM_ENGINE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/src/crypto_stat_arb/sim_engine")
pybind11_add_module(mm_matching_engine MODULE "${MM_ENGINE_DIR}/src/matching_engine_bindings.cpp")
target_include_directories(mm_matching_engine PRIVATE "${MM_ENGINE_DIR}/include")
target_compile_features(mm_matching_engine PRIVATE cxx_std_20)

# Build identity compiled into the binary (mm_matching_engine.__build_info__): the hash of
# the sources it was built from, computed exactly as maker_engine.cpp_source_sha256 does
# ("<relative path>:<sha256>\n" per file, sorted by path), plus the toolchain.
file(GLOB_RECURSE MM_ENGINE_SOURCES RELATIVE "${MM_ENGINE_DIR}"
     "${MM_ENGINE_DIR}/include/*.hpp" "${MM_ENGINE_DIR}/src/*.cpp")
list(SORT MM_ENGINE_SOURCES)
set(MM_ENGINE_MANIFEST "")
foreach(source IN LISTS MM_ENGINE_SOURCES)
  file(SHA256 "${MM_ENGINE_DIR}/${source}" source_hash)
  string(APPEND MM_ENGINE_MANIFEST "${source}:${source_hash}\n")
endforeach()
string(SHA256 MM_ENGINE_SOURCE_SHA256 "${MM_ENGINE_MANIFEST}")
target_compile_definitions(mm_matching_engine PRIVATE
  MM_BUILD_SOURCE_SHA256="${MM_ENGINE_SOURCE_SHA256}"
  MM_BUILD_COMPILER="${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}"
  MM_BUILD_TYPE="${CMAKE_BUILD_TYPE}"
  MM_BUILD_PYBIND11="${pybind11_VERSION}")

install(
  TARGETS mm_matching_engine
  LIBRARY DESTINATION crypto_stat_arb/sim_engine
  RUNTIME DESTINATION crypto_stat_arb/sim_engine
)
