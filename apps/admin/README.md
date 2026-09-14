# A2A Admin

Standalone Next.js admin console for the platform (`https://admin.<platform-domain>`).

Auth uses the shared A2A Cloud Keycloak realm. The admin app exchanges the
browser OIDC code, asks the control plane to verify the ID token, and only
creates an admin session when the linked `users.is_admin` flag is true.

The deployment reads:

- `A2A_PLATFORM_DOMAIN` (default `example.com`): every external link
  (`api.`, `app.`, `gitea.`, `registry.`, `argocd.`, `grafana.`, `auth.`) is derived
  from it. Override single hosts with `A2A_API_URL`, `A2A_DASHBOARD_URL`,
  `A2A_GITEA_URL`, `A2A_REGISTRY_HOST`, `A2A_ARGOCD_URL`, `A2A_GRAFANA_URL`,
  `A2A_INGRESS_HOST_TEMPLATE`.

- `ADMIN_SESSION_SECRET`
- `ADMIN_PUBLIC_URL` (default `https://admin.<platform-domain>`)
- `ADMIN_KEYCLOAK_REALM` (default `a2a`)
- `ADMIN_KEYCLOAK_ISSUER` (default `https://auth.<platform-domain>/realms/<realm>`)
- `ADMIN_KEYCLOAK_CLIENT_ID` (default `a2a-admin`)
- `A2A_CP_ADMIN_TOKEN`

Keep `ADMIN_SESSION_SECRET` and `A2A_CP_ADMIN_TOKEN` in the `admin-auth`
secret. The legacy username/password keys are ignored.

Rotate the session secret by regenerating `ADMIN_SESSION_SECRET` (any random
32+ byte string) in the `admin-auth` secret and restarting the pod.
