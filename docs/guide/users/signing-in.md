# Signing in

How you sign in depends on the mode your administrator chose. You can tell which
one you're in by what the home screen shows.

## Password sign-in

You'll see an **email + password** form.

- Enter the email and password your administrator gave you (or that you set).
- After signing in, you stay signed in for 7 days (a session cookie).
- **Five wrong passwords in a row locks the account for 15 minutes.** Wait it
  out, or ask an admin to reset your password.
- **Change your own password** any time in **Settings (⚙) → My Profile** — you'll
  need your current password.

If you forget your password, an admin can reset it for you (they'll give you a
new one shown once), or — for the owner — it can be reset from the command line.

## Google sign-in (OIDC)

You'll see a **"Sign in with Google"** button. Click it, choose your Google
account, and you're in. If your instance is restricted to a specific Google
Workspace domain, only accounts on that domain can sign in. Sign out from the
header.

## Behind Identity-Aware Proxy (IAP)

You won't see a Hippo login screen at all — Google's IAP handles sign-in before
the request reaches Hippo. Just open the URL; if you're allowed through IAP,
you're signed in.

## No sign-in (open mode)

Some personal or private-network instances run with no authentication. You go
straight to the chat and are treated as the owner (full access). This mode is
only appropriate on a trusted machine or private network.

## Your identity and role

Once signed in, the header shows your email and role. There are three roles:

- **user** — the default; sees user-tier content.
- **admin** — also manages folders/users and sees admin-tier content.
- **owner** — full access, including system configuration.

Your **email is your login identity and is read-only**. You can set a friendly
**display name** in Settings → My Profile. See
[Your profile & tokens](profile-and-tokens.md).
