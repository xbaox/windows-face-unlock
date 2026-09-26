cmake_minimum_required(VERSION 3.20)
project(FaceCredentialProvider LANGUAGES CXX RC)

# Stage 9 (R3, F-96): x64 only. LogonUI on the supported Windows builds is x64, the installer is
# x64-only, and an ARM64 or Win32 build of this DLL would register and then never load.
if(NOT CMAKE_GENERATOR_PLATFORM STREQUAL "x64" AND NOT CMAKE_VS_PLATFORM_NAME STREQUAL "x64")
    message(FATAL_ERROR "FaceCredentialProvider builds for x64 only: configure with -A x64 "
                        "(got platform '${CMAKE_GENERATOR_PLATFORM}${CMAKE_VS_PLATFORM_NAME}')")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Static CRT, on purpose. LogonUI loads this DLL from a System32 search context,
# so the only directory of ours that could ever satisfy an import is the one the
# DLL sits in -- {app}\credential_provider, which ships nothing but the DLL and
# register.ps1. With CMake's default (MultiThreadedDLL) the image imports
# VCRUNTIME140.dll, VCRUNTIME140_1.dll and MSVCP140.dll; none of the three is part
# of Windows, the installer does not carry the VC++ redistributable, and the frozen
# Python tree's _internal\ -- which does have them -- is not on this DLL's search
# path. A machine without the redistributable therefore fails to load it, mutely:
# the tile simply never appears, and SIGNING.md sends you looking at registration.
#
# This variable is honoured only under CMP0091=NEW, otherwise it is ignored in
# silence. The policy dates from CMake 3.15 and cmake_minimum_required(3.20) above
# already sets it NEW. Set BEFORE add_library(): the target reads it at creation.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

add_library(FaceCredentialProvider SHARED
    dll.cpp
    ClassFactory.cpp
    FaceCredentialProvider.cpp
    FaceCredential.cpp
    PipeClient.cpp
    helpers.cpp
    FaceCredentialProvider.def
    FaceCredentialProvider.rc
)

target_compile_definitions(FaceCredentialProvider PRIVATE
    UNICODE _UNICODE
    WIN32_LEAN_AND_MEAN
)

# /utf-8: the Russian tile texts in PipeClient.cpp are UTF-8 source (D-61).
# /guard:cf + /CETCOMPAT (R3, F-96): Control Flow Guard and CET shadow-stack compatibility for a
# DLL that runs inside LogonUI as SYSTEM.
target_compile_options(FaceCredentialProvider PRIVATE /utf-8 /guard:cf)
target_link_options(FaceCredentialProvider PRIVATE /guard:cf /CETCOMPAT)

# Stage 9 (D-63): credui, shlwapi and oleaut32 were linked with no symbol used from them.
target_link_libraries(FaceCredentialProvider PRIVATE
    secur32
    ole32
    advapi32
    user32
)

set_target_properties(FaceCredentialProvider PROPERTIES
    OUTPUT_NAME "FaceCredentialProvider"
    SUFFIX ".dll"
)
