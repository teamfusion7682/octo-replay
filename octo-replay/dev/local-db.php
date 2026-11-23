<?php
declare(strict_types=1);

/**
 * Yerel deneme için SQLite scouting-notları veritabanını oluşturur (MySQL gerekmez).
 * Kullanım (proje klasöründe):   php dev/local-db.php
 * Yalnızca komut satırından çalışır.
 */

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

if (!in_array('sqlite', PDO::getAvailableDrivers(), true)) {
    fwrite(STDERR, "PHP'de pdo_sqlite eklentisi kapalı. php.ini içinde 'extension=pdo_sqlite' satırını açın.\n");
    exit(1);
}

$dir = dirname(__DIR__) . '/cache';
if (!is_dir($dir) && !mkdir($dir, 0755, true) && !is_dir($dir)) {
    fwrite(STDERR, "cache/ klasörü oluşturulamadı.\n");
    exit(1);
}

$file = $dir . '/notes-local.sqlite';
$pdo  = new PDO('sqlite:' . $file, null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
$pdo->exec("
    CREATE TABLE IF NOT EXISTS scouting_notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        match_key   TEXT    NOT NULL,
        event_key   TEXT    NOT NULL,
        team_number INTEGER NULL,
        author_team INTEGER NOT NULL,
        author_name TEXT    NOT NULL DEFAULT '',
        category    TEXT    NOT NULL DEFAULT 'general',
        rating      INTEGER NULL,
        note        TEXT    NOT NULL,
        ip_hash     TEXT    NOT NULL,
        is_hidden   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
");
$pdo->exec('CREATE INDEX IF NOT EXISTS idx_match_visible ON scouting_notes (match_key, is_hidden, id)');
$pdo->exec('CREATE INDEX IF NOT EXISTS idx_rate_limit ON scouting_notes (ip_hash, created_at)');

echo "Hazır: {$file}\n\n";
echo "api/config.local.php içindeki 'db' bölümünü şununla değiştirin:\n\n";
echo "    'db' => ['dsn' => 'sqlite:' . dirname(__DIR__) . '/cache/notes-local.sqlite', 'user' => null, 'pass' => null],\n";
