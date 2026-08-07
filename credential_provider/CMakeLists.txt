cmake_minimum_required(VERSION 3.20)
project(FaceCredentialProvider LANGUAGES CXX)

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
)

target_compile_definitions(FaceCredentialProvider PRIVATE
    UNICODE _UNICODE
    WIN32_LEAN_AND_MEAN
)

target_link_libraries(FaceCredentialProvider PRIVATE
    credui
    secur32
    shlwapi
    ole32
    oleaut32
    advapi32
)

set_target_properties(FaceCredentialProvider PROPERTIES
    OUTPUT_NAME "FaceCredentialProvider"
    SUFFIX ".dll"
)
