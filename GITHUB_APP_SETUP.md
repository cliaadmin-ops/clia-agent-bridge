# GitHub App Setup Guide for CLIA Agent Bridge

This guide provides step-by-step instructions for creating and configuring a GitHub App to allow the CLIA Agent Bridge to interact with your repository.

## 1. Create the GitHub App

1.  Log in to GitHub and navigate to your Organization's settings (or your personal settings if not using an organization).
2.  In the left sidebar, scroll down to **Developer settings** > **GitHub Apps**.
3.  Click **New GitHub App**.
4.  **App Name:** Enter a name (e.g., `CLIA Agent Bridge`).
5.  **Homepage URL:** Enter your website URL (e.g., `https://canadaragolake.com`).
6.  **Webhook:** Uncheck **Active** (unless you specifically need webhooks for other features).
7.  **Permissions:**
    *   Under **Repository permissions**, find **Contents** and select **Read & write**.
    *   Under **Repository permissions**, find **Metadata** and select **Read-only** (this is usually mandatory and set by default).
8.  **Where can this GitHub App be installed?** Select **Only on this account** (unless you plan to use it across multiple organizations).
9.  Click **Create GitHub App**.

## 2. Generate a Private Key

1.  After creating the app, you will be taken to its settings page.
2.  Scroll down to the **Private keys** section.
3.  Click **Generate a private key**.
4.  A `.pem` file will be downloaded to your computer. **Keep this file secure.**

## 3. Install the App

1.  In the left sidebar of your App's settings, click **Install App**.
2.  Click **Install** next to your organization or account.
3.  Select **Only select repositories** and choose the `clia-website` repository.
4.  Click **Install**.

## 4. Collect Environment Variables

You need to provide the following three values to the CLIA Agent Bridge environment (e.g., Google Cloud Run):

### `GITHUB_APP_ID`
Found on the **General** tab of your GitHub App settings under **About**.

### `GITHUB_INSTALLATION_ID`
1.  Go to your Organization Settings > **GitHub Apps**.
2.  Click **Configure** next to your app.
3.  The Installation ID is the number at the end of the URL: `https://github.com/organizations/YOUR_ORG/settings/installations/12345678` (in this case, `12345678`).

### `GITHUB_PRIVATE_KEY`
This is the content of the `.pem` file you downloaded. 

**Important: Formatting for Cloud Run**
Google Cloud Run environment variables do not handle multi-line strings well. You must format the key as a single line by replacing all newlines with the literal characters `\n`.

**How to format:**
1.  Open the `.pem` file in a text editor.
2.  Replace every newline with `\n`.
3.  The result should look like this:
    `-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7...[lots of text]...\n-----END RSA PRIVATE KEY-----\n`
4.  Paste this single-line string into the `GITHUB_PRIVATE_KEY` environment variable.

The Agent Bridge code is configured to automatically convert these `\n` characters back into actual newlines at runtime.
