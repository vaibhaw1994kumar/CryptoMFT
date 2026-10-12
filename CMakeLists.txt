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
