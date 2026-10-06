# Google Calendar OAuth Setup

The SPA dashboard connects each spa's Google account through OAuth. Google
client credentials stay in the backend environment; they are never entered or
returned by the dashboard.

## Environment

Set these backend variables:

```text
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/spa-accounts/booking/google/callback
FRONTEND_BASE_URL=http://localhost:5173
```

For production, replace the localhost values with the public backend callback
and dashboard URL. The exact callback URL must match Google Cloud exactly.

## Google Cloud Console

1. Create or select a Google Cloud project.
2. Enable **Google Calendar API**.
3. Configure the OAuth consent screen and add the application users/test users
   while the app is in testing.
4. Create an OAuth client of type **Web application**.
5. Add the value of `GOOGLE_OAUTH_REDIRECT_URI` to **Authorized redirect URIs**.
6. Put the generated client ID and secret in the backend secret environment.
7. Run the database migration and open the SPA dashboard.

The dashboard flow is: connect Google, grant calendar access, select an
accessible calendar, and test the connection. The selected calendar ID is
stored in the existing encrypted booking configuration, while OAuth tokens are
stored in the per-SPA `google_calendar_connections` row using the existing
Fernet encryption helper.

The application requests Calendar access plus `openid` and `email` so it can
list calendars, create/update/delete events, and display the connected Google
account. No Google client secret or token is sent to the frontend.