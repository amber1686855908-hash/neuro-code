/* Gate 1.9 raw-entry evidence probe: no CRT, userenv, registry, or crypto. */

typedef void *HANDLE;
typedef unsigned long DWORD;
typedef int BOOL;

#define STD_OUTPUT_HANDLE ((DWORD)-11L)

__declspec(dllimport) HANDLE __stdcall GetStdHandle(DWORD handle);
__declspec(dllimport) BOOL __stdcall WriteFile(
    HANDLE handle,
    const void *buffer,
    DWORD bytes,
    DWORD *written,
    void *overlapped
);
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(unsigned int code);

void __stdcall gate19_raw_entry(void) {
    static const char marker[] = "W5_GATE19_PRAW_ENTRY\n";
    DWORD written = 0;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    if (output != (HANDLE)0 && output != (HANDLE)(long long)-1) {
        (void)WriteFile(output, marker, (DWORD)(sizeof(marker) - 1), &written, (void *)0);
    }
    ExitProcess(0);
}
