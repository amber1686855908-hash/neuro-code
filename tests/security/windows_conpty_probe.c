#define WIN32_LEAN_AND_MEAN

#include <windows.h>

#include <string.h>

static HANDLE input_handle(void) {
    return GetStdHandle(STD_INPUT_HANDLE);
}

static HANDLE output_handle(void) {
    return GetStdHandle(STD_OUTPUT_HANDLE);
}

static int write_bytes(const char *value, DWORD length) {
    DWORD written = 0;
    if (!WriteFile(output_handle(), value, length, &written, NULL)) {
        return 0;
    }
    return written == length;
}

static int write_text(const char *value) {
    return write_bytes(value, (DWORD)strlen(value));
}

static int write_uint(unsigned short value) {
    char digits[6];
    unsigned int index = sizeof(digits) - 1U;
    digits[index] = '\0';
    do {
        index--;
        digits[index] = (char)('0' + (value % 10U));
        value = (unsigned short)(value / 10U);
    } while (value != 0U);
    return write_bytes(&digits[index], (DWORD)(sizeof(digits) - 1U - index));
}

static int write_size(void) {
    CONSOLE_SCREEN_BUFFER_INFO info;
    if (!GetConsoleScreenBufferInfo(output_handle(), &info)) {
        return 0;
    }
    if (!write_text("W4_SIZE=") || !write_uint((unsigned short)info.dwSize.X)) {
        return 0;
    }
    if (!write_text("x") || !write_uint((unsigned short)info.dwSize.Y)) {
        return 0;
    }
    return write_text("\n");
}

static int valid_handle(HANDLE handle) {
    return handle != NULL && handle != INVALID_HANDLE_VALUE;
}

static int supports_console_api(HANDLE handle) {
    DWORD mode = 0;
    return valid_handle(handle) && GetConsoleMode(handle, &mode);
}

static int write_handle_observation(void) {
    HANDLE input = input_handle();
    HANDLE output = output_handle();
    HANDLE error = GetStdHandle(STD_ERROR_HANDLE);
    if (!valid_handle(input) || !valid_handle(output) || !valid_handle(error)) {
        return 0;
    }
    if (!write_text("W4_HANDLES=CONPTY;stdin-valid=1;stdout-valid=1;stderr-valid=1;")) {
        return 0;
    }
    if (!write_text(supports_console_api(input) ? "stdin-console=1;" : "stdin-console=0;")) {
        return 0;
    }
    if (!write_text(supports_console_api(output) ? "stdout-console=1;" : "stdout-console=0;")) {
        return 0;
    }
    if (!write_text(supports_console_api(error) ? "stderr-console=1\n" : "stderr-console=0\n")) {
        return 0;
    }
    return 1;
}

static int read_line(char *buffer, DWORD capacity) {
    DWORD used = 0;
    while (used + 1U < capacity) {
        char byte = 0;
        DWORD received = 0;
        if (!ReadFile(input_handle(), &byte, 1, &received, NULL) || received == 0U) {
            return 0;
        }
        if (byte == '\n' || byte == '\r') {
            buffer[used] = '\0';
            return 1;
        }
        buffer[used] = byte;
        used++;
    }
    return 0;
}

int main(void) {
    DWORD mode = 0;
    if (GetConsoleMode(input_handle(), &mode)) {
        mode &= ~(ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT);
        (void)SetConsoleMode(input_handle(), mode);
    }
    if (!write_handle_observation() || !write_text("W4_READY\n") || !write_size()) {
        return 91;
    }

    char line[128];
    for (;;) {
        if (!read_line(line, (DWORD)sizeof(line))) {
            return 92;
        }
        if (strcmp(line, "w4-input-token") == 0) {
            if (!write_text("W4_INPUT=w4-input-token\n")) {
                return 93;
            }
        } else if (strcmp(line, "w4-size") == 0) {
            if (!write_size()) {
                return 94;
            }
        } else if (strcmp(line, "w4-exit") == 0) {
            if (!write_text("W4_FINAL\n")) {
                return 95;
            }
            return 7;
        } else {
            if (!write_text("W4_UNKNOWN\n")) {
                return 96;
            }
        }
    }
}
