#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>

typedef LONG NTSTATUS;
typedef NTSTATUS(WINAPI *bcrypt_gen_random_fn)(
    void *algorithm_provider,
    unsigned char *buffer,
    unsigned long buffer_length,
    unsigned long flags);

static const unsigned long BCRYPT_USE_SYSTEM_PREFERRED_RNG = 0x00000002UL;

int main(void) {
    unsigned char buffer[32] = {0};
    HMODULE bcrypt = LoadLibraryW(L"bcrypt.dll");
    if (bcrypt == NULL) {
        printf("W5_BCRYPT_LOAD_FAILED=%lu\n", (unsigned long)GetLastError());
        return 2;
    }

    bcrypt_gen_random_fn gen_random =
        (bcrypt_gen_random_fn)(void *)GetProcAddress(bcrypt, "BCryptGenRandom");
    if (gen_random == NULL) {
        printf("W5_BCRYPT_SYMBOL_FAILED=%lu\n", (unsigned long)GetLastError());
        FreeLibrary(bcrypt);
        return 3;
    }

    NTSTATUS status = gen_random(NULL, buffer, (unsigned long)sizeof(buffer),
                                 BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    FreeLibrary(bcrypt);
    if (status != 0) {
        printf("W5_BCRYPT_STATUS=0x%08lx\n", (unsigned long)status);
        return 4;
    }
    puts("W5_BCRYPT_OK");
    return 0;
}
