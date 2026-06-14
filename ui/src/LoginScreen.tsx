type Props = {
  authMode: string;
  email: string;
  setEmail: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  error: string;
  onSubmit: (e: React.FormEvent) => void;
};

/** Sign-in screen shown when GET /me returns 401. Renders the password form in
 * password mode, otherwise the Google sign-in link (oidc). */
export default function LoginScreen({
  authMode, email, setEmail, password, setPassword, error, onSubmit,
}: Props) {
  return (
    <div className="app">
      <div className="empty signin">
        <span className="logo">{"\u{1F99B}"}</span>
        <h1>Hippo</h1>
        {authMode === "password" ? (
          <form className="login-form" onSubmit={onSubmit}>
            <input type="email" placeholder="email" value={email} autoFocus
              onChange={(e) => setEmail(e.target.value)} />
            <input type="password" placeholder="password" value={password}
              onChange={(e) => setPassword(e.target.value)} />
            <button className="upload-btn" type="submit">Sign in</button>
            {error && <p className="error">{error}</p>}
          </form>
        ) : (
          <>
            <p>Sign in with your Google account to continue.</p>
            <a className="upload-btn" href="/auth/login">Sign in with Google</a>
          </>
        )}
      </div>
    </div>
  );
}
