# Keycloak test realm

A synthetic realm for the campus sign-in tests (D5, D118): `tests/browser/test_sso_keycloak.py`
signs in through a real OpenID Connect provider without touching Google or a campus.

- Client `stacks`, secret `test-only-client-secret`. It accepts any redirect URI, which is only
  acceptable because this realm exists for tests.
- Every token carries `hd: example.edu`, as a Google Workspace account would.
- Users (password `a-long-test-password`): `avery` (verified `avery@example.edu`, has a Stacks
  account in the test), `casey` (email not verified) and `stranger` (verified, no Stacks account).

Run it next to the dev stack, then run the test with `KEYCLOAK_URL` pointing at it:

```bash
docker run -d --rm --name keycloak --network stacks_default \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  -v "$PWD/fixtures/keycloak:/opt/keycloak/data/import:ro" \
  quay.io/keycloak/keycloak:26.0.7 start-dev --import-realm
docker compose -f compose.yaml -f compose.dev.yaml run --rm --no-deps -T \
  -e KEYCLOAK_URL=http://keycloak:8080 web pytest -q tests/browser/test_sso_keycloak.py
```
