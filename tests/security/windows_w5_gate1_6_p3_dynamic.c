#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <bcrypt.h>
#include <ncrypt.h>
#include <stdio.h>

#pragma warning(disable: 4191)

typedef NTSTATUS (WINAPI *bcrypt_gen_random_fn)(
    void *,
    unsigned char *,
    ULONG,
    ULONG
);
typedef SECURITY_STATUS (WINAPI *ncrypt_open_storage_provider_fn)(
    NCRYPT_PROV_HANDLE *,
    LPCWSTR,
    DWORD
);
typedef SECURITY_STATUS (WINAPI *ncrypt_free_object_fn)(NCRYPT_HANDLE);

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

static void emit_status(const char *prefix, SECURITY_STATUS status) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)status);
    emit_ascii(line);
}

int main(void) {
    unsigned char buffer[32] = {0};
    HMODULE bcrypt = NULL;
    HMODULE ncrypt = NULL;
    bcrypt_gen_random_fn gen_random = NULL;
    ncrypt_open_storage_provider_fn open_provider = NULL;
    ncrypt_free_object_fn free_object = NULL;
    NCRYPT_PROV_HANDLE provider = 0;
    SECURITY_STATUS status;
    BOOL all_calls_succeeded = TRUE;

    emit_ascii("W5_GATE16_P3_STARTED\n");

    emit_ascii("W5_GATE16_BEFORE_LOAD_BCRYPT\n");
    SetLastError(ERROR_SUCCESS);
    bcrypt = LoadLibraryW(L"bcrypt.dll");
    if (bcrypt == NULL) {
        emit_ascii("W5_GATE16_BCRYPT_LOAD=FAIL\n");
        emit_u32("W5_GATE16_BCRYPT_LOAD_ERROR=", GetLastError());
        all_calls_succeeded = FALSE;
    } else {
        emit_ascii("W5_GATE16_BCRYPT_LOAD=PASS\n");
        gen_random = (bcrypt_gen_random_fn)GetProcAddress(bcrypt, "BCryptGenRandom");
        if (gen_random == NULL) {
            emit_ascii("W5_GATE16_BCRYPT_SYMBOL=FAIL\n");
            emit_u32("W5_GATE16_BCRYPT_SYMBOL_ERROR=", GetLastError());
            all_calls_succeeded = FALSE;
        } else {
            emit_ascii("W5_GATE16_BCRYPT_SYMBOL=PASS\n");
            emit_ascii("W5_GATE16_BEFORE_BCRYPT_CALL\n");
            status = gen_random(
                NULL,
                buffer,
                (ULONG)sizeof(buffer),
                BCRYPT_USE_SYSTEM_PREFERRED_RNG
            );
            emit_status("W5_GATE16_BCRYPT_STATUS=", status);
            if (status != 0) {
                all_calls_succeeded = FALSE;
            }
        }
        (void)FreeLibrary(bcrypt);
    }

    emit_ascii("W5_GATE16_BEFORE_LOAD_NCRYPT\n");
    SetLastError(ERROR_SUCCESS);
    ncrypt = LoadLibraryW(L"ncrypt.dll");
    if (ncrypt == NULL) {
        emit_ascii("W5_GATE16_NCRYPT_LOAD=FAIL\n");
        emit_u32("W5_GATE16_NCRYPT_LOAD_ERROR=", GetLastError());
        all_calls_succeeded = FALSE;
    } else {
        emit_ascii("W5_GATE16_NCRYPT_LOAD=PASS\n");
        open_provider = (ncrypt_open_storage_provider_fn)GetProcAddress(
            ncrypt,
            "NCryptOpenStorageProvider"
        );
        free_object = (ncrypt_free_object_fn)GetProcAddress(ncrypt, "NCryptFreeObject");
        if (open_provider == NULL || free_object == NULL) {
            emit_ascii("W5_GATE16_NCRYPT_SYMBOL=FAIL\n");
            emit_u32("W5_GATE16_NCRYPT_SYMBOL_ERROR=", GetLastError());
            all_calls_succeeded = FALSE;
        } else {
            emit_ascii("W5_GATE16_NCRYPT_SYMBOL=PASS\n");
            emit_ascii("W5_GATE16_BEFORE_NCRYPT_CALL\n");
            status = open_provider(&provider, MS_KEY_STORAGE_PROVIDER, 0);
            emit_status("W5_GATE16_NCRYPT_STATUS=", status);
            if (status == ERROR_SUCCESS && provider != 0) {
                (void)free_object(provider);
            } else {
                all_calls_succeeded = FALSE;
            }
        }
        (void)FreeLibrary(ncrypt);
    }

    emit_ascii("W5_GATE16_P3_FINISHED\n");
    return all_calls_succeeded ? 0 : 23;
}
