cmake_minimum_required(VERSION 3.16)
project(mm_matching_engine_bindings LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Python3 COMPONENTS Interpreter Development REQUIRED)
find_package(pybind11 CONFIG QUIET)

if(NOT pybind11_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        pybind11
        GIT_REPOSITORY https://github.com/pybind/pybind11.git
        GIT_TAG        v2.13.1
    )
    FetchContent_MakeAvailable(pybind11)
endif()

pybind11_add_module(mm_matching_engine src/matching_engine_bindings.cpp)
target_include_directories(mm_matching_engine PRIVATE include)
set_target_properties(mm_matching_engine PROPERTIES CXX_STANDARD 20)

# Output the .so / .pyd next to the Python files
set_target_properties(mm_matching_engine PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}"   # Windows .pyd
)
