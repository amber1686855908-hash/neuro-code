#include <windows.h>
#include <bcrypt.h>
#include <ncrypt.h>
#include <sddl.h>
#include <stdio.h>
#include <userenv.h>

#pragma comment(lib, "userenv.lib")
#pragma comment(lib, "Advapi32.lib")
#pragma comment(lib, "Bcrypt.lib")
#pragma comment(lib, "Ncrypt.lib")

static void emit_ascii(const char *text) {
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != NULL && output != INVALID_HANDLE_VALUE) {
        (void)WriteFile(output, text, (DWORD)lstrlenA(text), &written, NULL);
    }
}

static void emit_u32(const char *prefix, DWORD value) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s%lu\n", prefix, (unsigned long)value);
    emit_ascii(line);
}

static void emit_status(const char *prefix, SECURITY_STATUS value) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)value);
    emit_ascii(line);
}

static void emit_nul_probe(const char *name, DWORD access) {
    char prefix[128];
    HANDLE handle;
    DWORD error;
    DWORD written = 0;
    const char byte = 'x';

    (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_CREATE=", name);
    SetLastError(ERROR_SUCCESS);
    handle = CreateFileW(
        L"NUL",
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        NULL
    );
    if (handle == INVALID_HANDLE_VALUE) {
        emit_ascii(prefix);
        emit_ascii("FAIL\n");
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_CREATE_ERROR=", name);
        emit_u32(prefix, GetLastError());
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE=", name);
        emit_ascii(prefix);
        emit_ascii("NOT_ATTEMPTED\n");
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE_ERROR=", name);
        emit_u32(prefix, 0);
        return;
    }

    emit_ascii(prefix);
    emit_ascii("PASS\n");
    (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_CREATE_ERROR=", name);
    emit_u32(prefix, 0);

    if ((access & GENERIC_WRITE) == 0) {
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE=", name);
        emit_ascii(prefix);
        emit_ascii("NOT_ATTEMPTED\n");
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE_ERROR=", name);
        emit_u32(prefix, 0);
        (void)CloseHandle(handle);
        return;
    }

    SetLastError(ERROR_SUCCESS);
    if (WriteFile(handle, &byte, 1, &written, NULL)) {
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE=", name);
        emit_ascii(prefix);
        emit_ascii("PASS\n");
        error = 0;
    } else {
        (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE=", name);
        emit_ascii(prefix);
        emit_ascii("FAIL\n");
        error = GetLastError();
    }
    (void)snprintf(prefix, sizeof(prefix), "W5_GATE15_NUL_%s_WRITE_ERROR=", name);
    emit_u32(prefix, error);
    (void)CloseHandle(handle);
}

static void emit_profile_facts(HANDLE token) {
    wchar_t profile[32768];
    DWORD profile_length = (DWORD)(sizeof(profile) / sizeof(profile[0]));
    HANDLE current_user = NULL;
    HKEY hku_key = NULL;
    PTOKEN_USER token_user = NULL;
    LPWSTR sid_string = NULL;
    DWORD token_bytes = 0;
    DWORD error;
    LSTATUS status;

    SetLastError(ERROR_SUCCESS);
    if (GetUserProfileDirectoryW(token, profile, &profile_length)) {
        emit_ascii("W5_GATE15_PROFILE_DIRECTORY=AVAILABLE\n");
        emit_u32("W5_GATE15_PROFILE_DIRECTORY_ERROR=", 0);
    } else {
        emit_ascii("W5_GATE15_PROFILE_DIRECTORY=UNAVAILABLE\n");
        emit_u32("W5_GATE15_PROFILE_DIRECTORY_ERROR=", GetLastError());
    }

    SetLastError(ERROR_SUCCESS);
    (void)GetTokenInformation(token, TokenUser, NULL, 0, &token_bytes);
    error = GetLastError();
    if (token_bytes == 0 || error != ERROR_INSUFFICIENT_BUFFER) {
        emit_ascii("W5_GATE15_TOKEN_USER=ERROR\n");
        emit_u32("W5_GATE15_TOKEN_USER_ERROR=", error);
    } else {
        token_user = (PTOKEN_USER)LocalAlloc(LPTR, token_bytes);
        if (token_user == NULL ||
            !GetTokenInformation(token, TokenUser, token_user, token_bytes, &token_bytes)) {
            emit_ascii("W5_GATE15_TOKEN_USER=ERROR\n");
            emit_u32("W5_GATE15_TOKEN_USER_ERROR=", GetLastError());
        } else if (!ConvertSidToStringSidW(token_user->User.Sid, &sid_string)) {
            emit_ascii("W5_GATE15_TOKEN_USER=ERROR\n");
            emit_u32("W5_GATE15_TOKEN_USER_ERROR=", GetLastError());
        } else {
            emit_ascii("W5_GATE15_TOKEN_USER=PASS\n");
            emit_u32("W5_GATE15_TOKEN_USER_ERROR=", 0);
            status = RegOpenKeyExW(HKEY_USERS, sid_string, 0, KEY_READ, &hku_key);
            emit_u32("W5_GATE15_HKU_SID_STATUS=", (DWORD)status);
            emit_ascii(
                status == ERROR_SUCCESS ?
                    "W5_GATE15_HKU_SID=LOADED\n" :
                    "W5_GATE15_HKU_SID=NOT_LOADED\n"
            );
            if (hku_key != NULL) {
                (void)RegCloseKey(hku_key);
                hku_key = NULL;
            }
        }
    }

    status = RegOpenCurrentUser(KEY_READ, &current_user);
    emit_u32("W5_GATE15_CURRENT_USER_STATUS=", (DWORD)status);
    emit_ascii(
        status == ERROR_SUCCESS ?
            "W5_GATE15_CURRENT_USER=OPEN\n" :
            "W5_GATE15_CURRENT_USER=FAILED\n"
    );
    if (current_user != NULL) {
        (void)RegCloseKey(current_user);
    }
    if (sid_string != NULL) {
        (void)LocalFree(sid_string);
    }
    if (token_user != NULL) {
        (void)LocalFree(token_user);
    }
}

static void emit_crypto_facts(void) {
    HMODULE bcrypt = LoadLibraryW(L"bcrypt.dll");
    unsigned char random_buffer[32];
    wchar_t module_path[32768];
    DWORD module_length;
    NCRYPT_PROV_HANDLE provider = 0;
    SECURITY_STATUS status;

    if (bcrypt == NULL) {
        emit_ascii("W5_GATE15_BCRYPT_LIBRARY=FAILED\n");
        emit_u32("W5_GATE15_BCRYPT_LIBRARY_ERROR=", GetLastError());
        emit_ascii("W5_GATE15_BCRYPT_MODULE_PATH=NOT_ATTEMPTED\n");
        emit_u32("W5_GATE15_BCRYPT_MODULE_PATH_ERROR=", 0);
    } else {
        emit_ascii("W5_GATE15_BCRYPT_LIBRARY=LOADED\n");
        emit_u32("W5_GATE15_BCRYPT_LIBRARY_ERROR=", 0);
        SetLastError(ERROR_SUCCESS);
        module_length = GetModuleFileNameW(
            bcrypt,
            module_path,
            (DWORD)(sizeof(module_path) / sizeof(module_path[0]))
        );
        if (module_length == 0) {
            emit_ascii("W5_GATE15_BCRYPT_MODULE_PATH=UNAVAILABLE\n");
            emit_u32("W5_GATE15_BCRYPT_MODULE_PATH_ERROR=", GetLastError());
        } else {
            emit_ascii("W5_GATE15_BCRYPT_MODULE_PATH=AVAILABLE\n");
            emit_u32("W5_GATE15_BCRYPT_MODULE_PATH_ERROR=", 0);
        }
        (void)FreeLibrary(bcrypt);
    }

    status = BCryptGenRandom(
        NULL,
        random_buffer,
        (ULONG)sizeof(random_buffer),
        BCRYPT_USE_SYSTEM_PREFERRED_RNG
    );
    emit_status("W5_GATE15_BCRYPT_GEN_RANDOM_STATUS=", status);

    status = NCryptOpenStorageProvider(&provider, MS_KEY_STORAGE_PROVIDER, 0);
    emit_status("W5_GATE15_NCRYPT_OPEN_STATUS=", status);
    if (status == ERROR_SUCCESS && provider != 0) {
        (void)NCryptFreeObject(provider);
    }
}

int main(void) {
    HANDLE token = NULL;

    emit_ascii("W5_GATE15_PROBE_STARTED\n");

    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        emit_ascii("W5_GATE15_TOKEN=ERROR\n");
        emit_u32("W5_GATE15_TOKEN_ERROR=", GetLastError());
        emit_ascii("W5_GATE15_PROBE_FINISHED\n");
        return 20;
    }

    emit_ascii("W5_GATE15_TOKEN=PASS\n");
    emit_u32("W5_GATE15_TOKEN_ERROR=", 0);
    emit_profile_facts(token);
    emit_crypto_facts();
    emit_nul_probe("READ", GENERIC_READ);
    emit_nul_probe("WRITE", GENERIC_WRITE);
    emit_nul_probe("READ_WRITE", GENERIC_READ | GENERIC_WRITE);

    (void)CloseHandle(token);
    emit_ascii("W5_GATE15_PROBE_FINISHED\n");
    return 0;
}
