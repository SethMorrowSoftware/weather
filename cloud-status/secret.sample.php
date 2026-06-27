<?php
/**
 * Copy this file to "secret.php" and set a long random token that MATCHES the
 * controller's  status_push.token  (config.yaml or Settings → Remote status page).
 *
 * Keeping the token in a .php file means it is never served as text — if someone
 * requests secret.php directly, PHP executes it and returns a blank page.
 *
 * Generate a token, e.g.:  openssl rand -hex 32
 */
$INGEST_TOKEN = 'change-me-to-a-long-random-string';
