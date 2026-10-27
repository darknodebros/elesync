    document.getElementById('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('submit-btn');
      const errEl = document.getElementById('error');
      errEl.style.display = 'none';
      btn.disabled = true;
      btn.textContent = 'Signing in…';
      try {
        const res = await fetch('/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: document.getElementById('username').value,
            password: document.getElementById('password').value,
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: 'Sign in failed' }));
          throw new Error(err.error || 'Sign in failed');
        }
        window.location.href = '/';
      } catch (err) {
        errEl.textContent = err.message;
        errEl.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Sign in';
      }
    });
