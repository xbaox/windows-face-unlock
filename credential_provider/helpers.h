#pragma once
#include <windows.h>
#include <credentialprovider.h>
#include <ntsecapi.h>
#include <string>

namespace FaceUnlock {

// FIELD_STATE_PAIR is only declared in Microsoft's SampleCredentialProvider
// header set, not in the public Windows SDK. Define it ourselves.
struct FIELD_STATE_PAIR {
    CREDENTIAL_PROVIDER_FIELD_STATE cpfs;
    CREDENTIAL_PROVIDER_FIELD_INTERACTIVE_STATE cpfis;
};

// Field indices for our single-tile UI.
enum FIELD_ID : DWORD {
    FIELD_TILE_IMAGE = 0,
    FIELD_LABEL      = 1,
    FIELD_SUBMIT     = 2,
    FIELD_STATUS     = 3,
    FIELD_COUNT
};

extern const CREDENTIAL_PROVIDER_FIELD_DESCRIPTOR s_FieldDescriptors[FIELD_COUNT];
extern const FIELD_STATE_PAIR s_FieldStatePairs[FIELD_COUNT];

// Wraps username/password/domain into a KERB_INTERACTIVE_UNLOCK_LOGON that the
// LogonUI expects in CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION.rgbSerialization.
// Caller must CoTaskMemFree(*ppSerialization).
HRESULT KerbPackInteractiveUnlock(const std::wstring& domain,
                                  const std::wstring& username,
                                  const std::wstring& password,
                                  CREDENTIAL_PROVIDER_USAGE_SCENARIO cpus,
                                  CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs);

void KerbUnpackFree(CREDENTIAL_PROVIDER_CREDENTIAL_SERIALIZATION* pcpcs);

// 8b F-24: g_DllRefs was never incremented -> DllCanUnloadNow always said S_OK, so the DLL
// could be unmapped under live objects -> every COM object and LockServer holds a DLL ref.
// Defined in dll.cpp.
void DllAddRef();
void DllRelease();

// 8b F-48: C++ exceptions (std::bad_alloc, std::system_error) could escape a COM method into
// LogonUI -> std::terminate -> translate at the boundary. Call ONLY from inside a catch block:
// bad_alloc -> E_OUTOFMEMORY, anything else -> E_UNEXPECTED.
HRESULT HResultFromCurrentException() noexcept;

}  // namespace FaceUnlock
