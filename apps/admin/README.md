# A2A Admin

Standalone Next.js admin console for `https://admin.a2acloud.io`.

Auth uses the shared A2A Cloud Keycloak realm. The admin app exchanges the
browser OIDC code, asks the control plane to verify the ID token, and only
creates an admin session when the linked `users.is_admin` flag is true.

The deployment reads:

- `ADMIN_SESSION_SECRET`
- `ADMIN_PUBLIC_URL`
- `ADMIN_KEYCLOAK_ISSUER`
- `ADMIN_KEYCLOAK_CLIENT_ID`
- `A2A_CP_ADMIN_TOKEN`

Keep `ADMIN_SESSION_SECRET` and `A2A_CP_ADMIN_TOKEN` in the `admin-auth`
secret. The legacy username/password keys are ignored.

Rotate the session secret by regenerating `ADMIN_SESSION_SECRET` (any random
32+ byte string) in the `admin-auth` secret and restarting the pod.
