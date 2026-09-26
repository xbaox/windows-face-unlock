#include <windows.h>
#include <unknwn.h>
#include <objbase.h>
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

// Registration helpers: called via regsvr32 by the installer (and credential_provider/register.ps1).
static HRESULT WriteReg(HKEY root, PCWSTR sub, PCWSTR name, PCWSTR value) {
    HKEY k;
    LONG r = RegCreateKeyExW(root, sub, 0, nullptr, 0, KEY_WRITE | KEY_WOW64_64KEY, nullptr, &k,
                             nullptr);
    if (r != ERROR_SUCCESS) return HRESULT_FROM_WIN32(r);
    r = RegSetValueExW(k, name, 0, REG_SZ,
                       (const BYTE*)value, (DWORD)((wcslen(value) + 1) * sizeof(WCHAR)));
    RegCloseKey(k);
    return HRESULT_FROM_WIN32(r);
}

// Stage 9 (D-64): the CLSID string was a literal repeated in four places -> derived from guid.h.
static bool ClsidString(WCHAR (&out)[64]) {
    return StringFromGUID2(CLSID_FaceCredentialProvider, out, 64) > 0;
}

// Stage 9 (F-97): the class went to the merged HKEY_CLASSES_ROOT view, which lands in HKCU when the
// elevating admin already has a per-user CLSID key -- and LogonUI (SYSTEM) never reads that. The
// class now always goes to HKLM\SOFTWARE\Classes.
// 8b F-48: stop at, and return, the first failing HRESULT; a truncated module path is refused.
STDAPI DllRegisterServer(void) {
    WCHAR path[MAX_PATH];
    const DWORD n = GetModuleFileNameW((HMODULE)&__ImageBase, path, MAX_PATH);
    if (n == 0) return HRESULT_FROM_WIN32(GetLastError());
    if (n >= MAX_PATH) return HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);

    WCHAR clsid[64];
    if (!ClsidString(clsid)) return E_UNEXPECTED;
    WCHAR sub[MAX_PATH];
    HRESULT hr;

    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s", clsid);
    hr = WriteReg(HKEY_LOCAL_MACHINE, sub, nullptr, L"Face Unlock Credential Provider");
    if (FAILED(hr)) return hr;

    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s\\InprocServer32", clsid);
    hr = WriteReg(HKEY_LOCAL_MACHINE, sub, nullptr, path);
    if (FAILED(hr)) return hr;
    hr = WriteReg(HKEY_LOCAL_MACHINE, sub, L"ThreadingModel", L"Apartment");
    if (FAILED(hr)) return hr;

    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    hr = WriteReg(HKEY_LOCAL_MACHINE, sub, nullptr, L"Face Unlock Credential Provider");
    if (FAILED(hr)) return hr;
    return S_OK;
}

STDAPI DllUnregisterServer(void) {
    WCHAR clsid[64];
    if (!ClsidString(clsid)) return E_UNEXPECTED;
    WCHAR sub[MAX_PATH];
    wsprintfW(sub,
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Authentication\\Credential Providers\\%s",
        clsid);
    RegDeleteKeyExW(HKEY_LOCAL_MACHINE, sub, KEY_WOW64_64KEY, 0);
    wsprintfW(sub, L"SOFTWARE\\Classes\\CLSID\\%s", clsid);
    HKEY k;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, sub, 0, KEY_ALL_ACCESS | KEY_WOW64_64KEY, &k) == ERROR_SUCCESS) {
        RegDeleteTreeW(k, nullptr);
        RegCloseKey(k);
        RegDeleteKeyExW(HKEY_LOCAL_MACHINE, sub, KEY_WOW64_64KEY, 0);
    }
    // A pre-9 registration may also have left the class in the merged view: clean it too.
    wsprintfW(sub, L"CLSID\\%s", clsid);
    RegDeleteTreeW(HKEY_CLASSES_ROOT, sub);
    return S_OK;
}
