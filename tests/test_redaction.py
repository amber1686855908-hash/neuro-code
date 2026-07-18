from neuro_code.redaction import redact_sensitive_text


def test_redacts_explicit_values_and_common_credential_shapes() -> None:
    rendered = redact_sensitive_text(
        (
            "explicit=provider-secret\n"
            'API_KEY="sk-examplecredential123"\n'
            "Authorization: Bearer abcdefghijklmnop\n"
            "proxy=https://user:proxy-password@example.test/path\n"
            "plain text remains"
        ),
        explicit_values=("provider-secret",),
    )

    assert "provider-secret" not in rendered
    assert "sk-examplecredential123" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert "proxy-password" not in rendered
    assert rendered.count("[REDACTED]") >= 4
    assert "plain text remains" in rendered


def test_does_not_hide_non_secret_token_counters_or_environment_key_names() -> None:
    source = (
        "output_tokens = completion.output_tokens or 0\n"
        'api_key_env = "DEEPSEEK_API_KEY"\n'
        "DEEPSEEK_API_KEY=sk-examplecredential123"
    )

    rendered = redact_sensitive_text(source)

    assert "output_tokens = completion.output_tokens or 0" in rendered
    assert 'api_key_env = "DEEPSEEK_API_KEY"' in rendered
    assert "sk-examplecredential123" not in rendered
