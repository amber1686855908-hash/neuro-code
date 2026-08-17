#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif

#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <sddl.h>
#include <stdio.h>
#include <userenv.h>
#include <wchar.h>

#pragma comment(lib, "Advapi32.lib")
#pragma comment(lib, "Userenv.lib")

static void g21_emit(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE && text != NULL) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void g21_emit_u32(const char *prefix, DWORD value) {
    char line[160];
    (void)snprintf(line, sizeof(line), "%s%lu\n", prefix, (unsigned long)value);
    g21_emit(line);
}

static void g21_emit_hresult(const char *prefix, HRESULT value) {
    char line[160];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)value);
    g21_emit(line);
}

static void g21_emit_wide(const char *prefix, const wchar_t *value) {
    char converted[1024];
    char line[1200];
    int written;
    int length;
    if (value == NULL || value[0] == L'\0') {
        (void)snprintf(line, sizeof(line), "%sUNSET\n", prefix);
        g21_emit(line);
        return;
    }
    length = WideCharToMultiByte(
        CP_UTF8,
        WC_ERR_INVALID_CHARS,
        value,
        -1,
        converted,
        (int)sizeof(converted),
        NULL,
        NULL
    );
    if (length <= 0) {
        (void)snprintf(line, sizeof(line), "%sUNAVAILABLE\n", prefix);
        g21_emit(line);
        return;
    }
    converted[sizeof(converted) - 1] = '\0';
    written = _snprintf_s(line, sizeof(line), _TRUNCATE, "%s%s\n", prefix, converted);
    if (written > 0) {
        g21_emit(line);
    }
}

static BOOL g21_environment_value(const wchar_t *name, wchar_t *value, DWORD capacity) {
    DWORD length;
    if (name == NULL || value == NULL || capacity == 0) {
        return FALSE;
    }
    SetLastError(ERROR_SUCCESS);
    length = GetEnvironmentVariableW(name, value, capacity);
    if (length == 0) {
        value[0] = L'\0';
        return FALSE;
    }
    if (length >= capacity) {
        value[0] = L'\0';
        return FALSE;
    }
    return TRUE;
}

static BOOL g21_path_exists(const wchar_t *path) {
    DWORD attributes;
    if (path == NULL || path[0] == L'\0') {
        return FALSE;
    }
    attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES;
}

static BOOL g21_token_user_sid(HANDLE token, PTOKEN_USER *user_out, LPWSTR *sid_text) {
    DWORD required = 0;
    PTOKEN_USER user = NULL;
    LPWSTR converted = NULL;
    if (user_out == NULL || sid_text == NULL) {
        return FALSE;
    }
    *user_out = NULL;
    *sid_text = NULL;
    SetLastError(ERROR_SUCCESS);
    (void)GetTokenInformation(token, TokenUser, NULL, 0, &required);
    if (required == 0 || GetLastError() != ERROR_INSUFFICIENT_BUFFER) {
        return FALSE;
    }
    user = (PTOKEN_USER)LocalAlloc(LPTR, required);
    if (user == NULL || !GetTokenInformation(token, TokenUser, user, required, &required)) {
        if (user != NULL) {
            (void)LocalFree(user);
        }
        return FALSE;
    }
    if (!ConvertSidToStringSidW(user->User.Sid, &converted)) {
        (void)LocalFree(user);
        return FALSE;
    }
    *user_out = user;
    *sid_text = converted;
    /* The TOKEN_USER allocation remains owned by the caller through user_out. */
    return TRUE;
}

static void g21_emit_profile_facts(HANDLE token) {
    wchar_t buffer[32768];
    wchar_t environment_value[32768];
    DWORD length = (DWORD)(sizeof(buffer) / sizeof(buffer[0]));
    DWORD error;
    HKEY current_user = NULL;
    HKEY hku_key = NULL;
    PTOKEN_USER token_user = NULL;
    LPWSTR sid_text = NULL;
    LSTATUS status;
    DWORD username_length = (DWORD)(sizeof(buffer) / sizeof(buffer[0]));

    if (GetUserNameW(buffer, &username_length)) {
        g21_emit_wide("W5_GATE21_USERNAME=", buffer);
    } else {
        g21_emit("W5_GATE21_USERNAME=UNAVAILABLE\n");
    }

    if (g21_token_user_sid(token, &token_user, &sid_text)) {
        g21_emit_wide("W5_GATE21_TOKEN_USER_SID=", sid_text);
        status = RegOpenKeyExW(HKEY_USERS, sid_text, 0, KEY_READ, &hku_key);
        g21_emit_u32("W5_GATE21_HKU_SID_STATUS=", (DWORD)status);
        g21_emit((status == ERROR_SUCCESS) ?
            "W5_GATE21_HKU_SID=LOADED\n" :
            "W5_GATE21_HKU_SID=NOT_LOADED\n");
        if (hku_key != NULL) {
            (void)RegCloseKey(hku_key);
        }
    } else {
        g21_emit("W5_GATE21_TOKEN_USER_SID=UNAVAILABLE\n");
        g21_emit_u32("W5_GATE21_HKU_SID_STATUS=", ERROR_INVALID_DATA);
        g21_emit("W5_GATE21_HKU_SID=UNKNOWN\n");
    }

    length = (DWORD)(sizeof(buffer) / sizeof(buffer[0]));
    SetLastError(ERROR_SUCCESS);
    if (GetUserProfileDirectoryW(token, buffer, &length)) {
        g21_emit("W5_GATE21_PROFILE_DIRECTORY=AVAILABLE\n");
        g21_emit_u32("W5_GATE21_PROFILE_DIRECTORY_ERROR=", 0);
        g21_emit_wide("W5_GATE21_PROFILE_DIRECTORY_PATH=", buffer);
        g21_emit(g21_path_exists(buffer) ?
            "W5_GATE21_PROFILE_DIRECTORY_EXISTS=YES\n" :
            "W5_GATE21_PROFILE_DIRECTORY_EXISTS=NO\n");
    } else {
        error = GetLastError();
        g21_emit("W5_GATE21_PROFILE_DIRECTORY=UNAVAILABLE\n");
        g21_emit_u32("W5_GATE21_PROFILE_DIRECTORY_ERROR=", error);
        g21_emit("W5_GATE21_PROFILE_DIRECTORY_PATH=UNAVAILABLE\n");
        g21_emit("W5_GATE21_PROFILE_DIRECTORY_EXISTS=UNKNOWN\n");
    }

    if (g21_environment_value(L"USERPROFILE", environment_value, (DWORD)(sizeof(environment_value) / sizeof(environment_value[0])))) {
        g21_emit_wide("W5_GATE21_ENV_USERPROFILE=", environment_value);
    } else {
        g21_emit("W5_GATE21_ENV_USERPROFILE=UNSET\n");
    }
    if (g21_environment_value(L"LOCALAPPDATA", environment_value, (DWORD)(sizeof(environment_value) / sizeof(environment_value[0])))) {
        g21_emit_wide("W5_GATE21_ENV_LOCALAPPDATA=", environment_value);
        g21_emit(g21_path_exists(environment_value) ?
            "W5_GATE21_LOCALAPPDATA_EXISTS=YES\n" :
            "W5_GATE21_LOCALAPPDATA_EXISTS=NO\n");
    } else {
        g21_emit("W5_GATE21_ENV_LOCALAPPDATA=UNSET\n");
        g21_emit("W5_GATE21_LOCALAPPDATA_EXISTS=UNKNOWN\n");
    }
    if (g21_environment_value(L"APPDATA", environment_value, (DWORD)(sizeof(environment_value) / sizeof(environment_value[0])))) {
        g21_emit_wide("W5_GATE21_ENV_APPDATA=", environment_value);
    } else {
        g21_emit("W5_GATE21_ENV_APPDATA=UNSET\n");
    }
    if (g21_environment_value(L"TEMP", environment_value, (DWORD)(sizeof(environment_value) / sizeof(environment_value[0])))) {
        g21_emit_wide("W5_GATE21_ENV_TEMP=", environment_value);
    } else {
        g21_emit("W5_GATE21_ENV_TEMP=UNSET\n");
    }
    if (g21_environment_value(L"TMP", environment_value, (DWORD)(sizeof(environment_value) / sizeof(environment_value[0])))) {
        g21_emit_wide("W5_GATE21_ENV_TMP=", environment_value);
    } else {
        g21_emit("W5_GATE21_ENV_TMP=UNSET\n");
    }

    status = RegOpenCurrentUser(KEY_READ, &current_user);
    g21_emit_u32("W5_GATE21_CURRENT_USER_STATUS=", (DWORD)status);
    g21_emit((status == ERROR_SUCCESS) ?
        "W5_GATE21_CURRENT_USER=OPEN\n" :
        "W5_GATE21_CURRENT_USER=FAILED\n");
    if (current_user != NULL) {
        (void)RegCloseKey(current_user);
    }

    if (sid_text != NULL) {
        (void)LocalFree(sid_text);
    }
    if (token_user != NULL) {
        (void)LocalFree(token_user);
    }
}

static void g21_profile_probe(const wchar_t *requested_name) {
    WCHAR name[64];
    PSID profile_sid = NULL;
    PSID derived_sid = NULL;
    HRESULT created;
    HRESULT derived;
    HRESULT deleted;
    DWORD pid = GetCurrentProcessId();
    DWORD tick = GetTickCount();
    BOOL sid_match = FALSE;

    if (requested_name != NULL && requested_name[0] != L'\0') {
        if (wcsncpy_s(name, sizeof(name) / sizeof(name[0]), requested_name, _TRUNCATE) != 0) {
            name[0] = L'\0';
        }
    } else if (swprintf_s(name, sizeof(name) / sizeof(name[0]),
        L"NeuroCodeW5A1-%lu-%lu", (unsigned long)pid, (unsigned long)tick) < 0) {
        name[0] = L'\0';
    }
    if (name[0] == L'\0' || wcslen(name) > 64) {
        g21_emit("W5_GATE21_PROFILE_CREATE=FAIL\n");
        g21_emit_hresult("W5_GATE21_PROFILE_CREATE_HRESULT=", E_INVALIDARG);
        g21_emit("W5_GATE21_PROFILE_DELETE=NOT_CREATED\n");
        return;
    }
    g21_emit_wide("W5_GATE21_PROFILE_NAME=", name);
    g21_emit("W5_GATE21_PROFILE_ARGUMENTS=CAPABILITIES_NULL;COUNT_0\n");
    created = CreateAppContainerProfile(name, name, L"Neuro Code W5 Gate 2A.1", NULL, 0, &profile_sid);
    g21_emit_hresult("W5_GATE21_PROFILE_CREATE_HRESULT=", created);
    if (SUCCEEDED(created) && profile_sid != NULL) {
        g21_emit("W5_GATE21_PROFILE_CREATE=PASS\n");
        {
            LPWSTR profile_text = NULL;
            if (ConvertSidToStringSidW(profile_sid, &profile_text)) {
                g21_emit_wide("W5_GATE21_PROFILE_SID=", profile_text);
                (void)LocalFree(profile_text);
            } else {
                g21_emit("W5_GATE21_PROFILE_SID=UNAVAILABLE\n");
            }
        }
    } else {
        g21_emit("W5_GATE21_PROFILE_CREATE=FAIL\n");
        if (created == HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)) {
            derived = DeriveAppContainerSidFromAppContainerName(name, &derived_sid);
            g21_emit_hresult("W5_GATE21_PROFILE_DERIVE_HRESULT=", derived);
            if (SUCCEEDED(derived) && derived_sid != NULL) {
                g21_emit("W5_GATE21_PROFILE_EXISTING_DERIVED=PASS\n");
            } else {
                g21_emit("W5_GATE21_PROFILE_EXISTING_DERIVED=FAIL\n");
            }
        }
    }

    if (profile_sid != NULL) {
        derived = DeriveAppContainerSidFromAppContainerName(name, &derived_sid);
        g21_emit_hresult("W5_GATE21_PROFILE_DERIVE_HRESULT=", derived);
        if (SUCCEEDED(derived) && derived_sid != NULL) {
            sid_match = EqualSid(profile_sid, derived_sid);
        }
        g21_emit(sid_match ?
            "W5_GATE21_PROFILE_DERIVED_SID_MATCH=PASS\n" :
            "W5_GATE21_PROFILE_DERIVED_SID_MATCH=FAIL\n");
    }

    if (profile_sid != NULL || SUCCEEDED(created)) {
        deleted = DeleteAppContainerProfile(name);
        g21_emit_hresult("W5_GATE21_PROFILE_DELETE_HRESULT=", deleted);
        g21_emit(SUCCEEDED(deleted) ?
            "W5_GATE21_PROFILE_DELETE=PASS\n" :
            "W5_GATE21_PROFILE_DELETE=FAIL\n");
    } else {
        g21_emit("W5_GATE21_PROFILE_DELETE=NOT_CREATED\n");
    }
    if (profile_sid != NULL) {
        (void)FreeSid(profile_sid);
    }
    if (derived_sid != NULL) {
        (void)FreeSid(derived_sid);
    }
}

int wmain(int argc, wchar_t **argv) {
    HANDLE token = NULL;
    g21_emit("W5_GATE21_PROBE_STARTED\n");
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        g21_emit_u32("W5_GATE21_TOKEN_OPEN_ERROR=", GetLastError());
        g21_emit("W5_GATE21_PROBE_FINISHED\n");
        return 20;
    }
    g21_emit_profile_facts(token);
    g21_profile_probe(argc > 1 ? argv[1] : NULL);
    (void)CloseHandle(token);
    g21_emit("W5_GATE21_PROBE_FINISHED\n");
    return 0;
}
