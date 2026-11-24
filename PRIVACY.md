# ghsync privacy policy

ghsync is a personal, self-hosted tool. Its only user is its owner.

- **What it accesses:** read-only Google Health data (activity, health metrics, sleep) for the owner's own Google account, through the Google Health API.
- **Where data goes:** to a SQLite database on the owner's own server. Nothing is sent to third parties, sold, shared, or used for advertising.
- **Storage and deletion:** data and OAuth tokens stay on that server. Deleting the `data/` folder removes them. Access can be revoked at any time at https://myaccount.google.com/permissions.
- **Google API Services User Data Policy:** ghsync's use of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.
