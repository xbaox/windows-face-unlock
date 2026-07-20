cmake_minimum_required(VERSION 3.20)
project(FaceCPTests LANGUAGES CXX)

# Standalone test/harness build for the Face Unlock Credential Provider.
# Not part of the DLL build -- configure this directory on its own:
#   cmake -B build-tests -S credential_provider/tests -A x64
#   cmake --build build-tests --config Release
#
# /utf-8 is required: test_parser.cpp carries UTF-8 wide-string literals used as
# expected-decode values (e.g. L"café").

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Offline parser unit test (camera-free, pipe-free, service-free).
add_executable(test_parser
    test_parser.cpp
    ../PipeClient.cpp
)
target_compile_definitions(test_parser PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN)
target_compile_options(test_parser PRIVATE /utf-8)
target_link_libraries(test_parser PRIVATE advapi32)

# Live unlock harness (talks to a running FaceService; prints only a safe,
# password-masked summary). Guarded so this file also configures before the
# harness source is added.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/unlock_harness.cpp")
    add_executable(unlock_harness
        unlock_harness.cpp
        ../PipeClient.cpp
    )
    target_compile_definitions(unlock_harness PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN)
    target_compile_options(unlock_harness PRIVATE /utf-8)
    target_link_libraries(unlock_harness PRIVATE advapi32)
endif()
