#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0600
#endif

#include <windows.h>
#include <bcrypt.h>
#include <stdio.h>

#pragma warning(disable: 4191)

typedef NTSTATUS(WINAPI *bcrypt_gen_random_fn)(
    void *,
    unsigned char *,
    ULONG,
    ULONG
);

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

static void emit_status(const char *prefix, NTSTATUS status) {
    char line[128];
    (void)snprintf(line, sizeof(line), "%s0x%08lX\n", prefix, (unsigned long)status);
    emit_ascii(line);
}

int main(void) {
    unsigned char buffer[32] = {0};
    HMODULE bcrypt = NULL;
    bcrypt_gen_random_fn gen_random = NULL;
    NTSTATUS status = (NTSTATUS)0xC0000001L;
    BOOL success = TRUE;

    emit_ascii("W5_GATE16_P4_STARTED\n");
    emit_ascii("W5_GATE16_P4_BEFORE_LOAD_BCRYPT\n");
    SetLastError(ERROR_SUCCESS);
    bcrypt = LoadLibraryW(L"bcrypt.dll");
    if (bcrypt == NULL) {
        emit_ascii("W5_GATE16_P4_BCRYPT_LOAD=FAIL\n");
        emit_u32("W5_GATE16_P4_BCRYPT_LOAD_ERROR=", GetLastError());
        success = FALSE;
    } else {
        emit_ascii("W5_GATE16_P4_BCRYPT_LOAD=PASS\n");
        gen_random = (bcrypt_gen_random_fn)GetProcAddress(bcrypt, "BCryptGenRandom");
        if (gen_random == NULL) {
            emit_ascii("W5_GATE16_P4_BCRYPT_SYMBOL=FAIL\n");
            emit_u32("W5_GATE16_P4_BCRYPT_SYMBOL_ERROR=", GetLastError());
            success = FALSE;
        } else {
            emit_ascii("W5_GATE16_P4_BCRYPT_SYMBOL=PASS\n");
            emit_ascii("W5_GATE16_P4_BEFORE_BCRYPT_CALL\n");
            status = gen_random(
                NULL,
                buffer,
                (ULONG)sizeof(buffer),
                BCRYPT_USE_SYSTEM_PREFERRED_RNG
            );
            emit_status("W5_GATE16_P4_BCRYPT_STATUS=", status);
            if (status != 0) {
                success = FALSE;
            }
        }
        (void)FreeLibrary(bcrypt);
    }

    emit_ascii("W5_GATE16_P4_FINISHED\n");
    return success ? 0 : 24;
}
