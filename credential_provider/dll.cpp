#include <windows.h>
#include <unknwn.h>
#include "guid.h"

EXTERN_C IMAGE_DOS_HEADER __ImageBase;

namespace FaceUnlock {
HRESULT CreateClassFactory(REFIID riid, void** ppv);
}

static LONG g_DllRefs = 0;

namespace FaceUnlock {
// 8b F-24: nothing ever incremented g_DllRefs -> DllCanUnloadNow always answered S_OK and
// COM could unmap the DLL under live objects (or a still-running worker) -> the provider, the
// credential and the class factory hold a ref for their lifetime, and LockServer counts too.
void DllAddRef()  { InterlockedIncrement(&g_DllRefs); }
void DllRelease() { InterlockedDecrement(&g_DllRefs); }
}

// 8b F-48: DllMain called DisableThreadLibraryCalls(nullptr) -> a no-op (null module handle)
// that invited a "fix" -> removed. Since 2f13b81 the DLL links the static CRT (/MT), and a DLL
// on the static CRT must NOT call DisableThreadLibraryCalls: the CRT needs its thread
// notifications. Do not reintroduce it with a real module handle.
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) {
    return TRUE;
}

STDAPI DllCanUnloadNow(void) {
    // Interlocked read: the refs are changed with Interlocked ops on other threads.
    return (InterlockedCompareExchange(&g_DllRefs, 0, 0) == 0) ? S_OK : S_FALSE;
}

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv) {
    if (rclsid != CLSID_FaceCredentialProvider) return CLASS_E_CLASSNOTAVAILABLE;
    return FaceUnlock::CreateClassFactory(riid, ppv);
}

// Registration helpers: called via regsvr32. In production prefer an MSI.
static HRESULT WriteReg(HKEY root, PCWSTR sub, PCWSTR name, PCWSTR value) {
    HKEY k;
    LONG r = RegCreateKeyExW(root, sub, 0, nullptr, 0, KEY_WRITE, nullptr, &k, nullptr);
    if (r != ERROR_SUCCESS) return HRESULT_FROM_WIN32(r);
    r = RegSetValueExW(k, name, 0, REG_SZ,
                       (const BYTE*)value, (DWORD)((wcslen(value) + 1) * sizeof(WCHAR)));
    RegCloseKey(k);
    return HRESULT_FROM_WIN32(r);
}

// 8b F-48: every WriteReg result was ignored and S_OK returned -> a partial registration
// reported success and the tile silently never appeared -> stop at, and return, the first
// failing HRESULT. A truncated module path is refused rather than registered. The keys and
// values written are unchanged.
STDAPI DllRegisterServer(void) {
    WCHAR path[MAX_PATH];
    const DWORD n = GetModuleFileNameW((HMODULE)&__ImageBase, path, MAX_PATH);
    if (n == 0) return HRESULT_FROM_WIN32(GetLastError());
    if (n >= MAX_PATH) return HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);

    const WCHAR* clsid = L"{8414D7B6-D536-461B-B31B-ADF77B3A8974}";
    WCHAR sub[MAX_PATH];
    HRESULT hr;

    wsprintfW(sub, L"CLSID\\%s", clsid);
    hr = WriteReg(HKEY_CLASSES_ROOT, sub, nullptr, L"Face Unlock Credential Provider");
    if (FAILED(hr)) return hr;

    wsprintfW(sub, L"CLSID\\%s\\InprocServer32", clsid);
    hr = WriteReg(HKEY_CLASSES_ROOT, sub, nullptr, path);
    if (FAILED(hr)) return hr;
    hr = WriteReg(HKEY_CLASSES_ROOT, sub, L"ThreadingModel", L"Apartment");
    if (FAILED(hr)) return hr;

    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    hr = WriteReg(HKEY_LOCAL_MACHINE, sub, nullptr, L"Face Unlock Credential Provider");
    if (FAILED(hr)) return hr;
    return S_OK;
}

STDAPI DllUnregisterServer(void) {
    const WCHAR* clsid = L"{8414D7B6-D536-461B-B31B-ADF77B3A8974}";
    WCHAR sub[MAX_PATH];
    wsprintfW(sub, L"CLSID\\%s", clsid);
    RegDeleteTreeW(HKEY_CLASSES_ROOT, sub);
    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, sub);
    return S_OK;
}
