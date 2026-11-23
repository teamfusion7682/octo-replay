<?php
declare(strict_types=1);

/**
 * Octo-Replay · Sunucu yapılandırması
 *
 * Gizli değerleri (TBA anahtarı, veritabanı parolası) bu dosyayı düzenlemek yerine
 * aynı klasörde oluşturacağınız `config.local.php` dosyasında tanımlayın:
 *
 *   <?php
 *   return [
 *       'tba_api_key' => 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
 *       'db' => ['host' => 'localhost', 'name' => 'hesap_octo', 'user' => 'hesap_octo', 'pass' => '...'],
 *       'notes' => ['ip_salt' => 'uzun-rastgele-bir-dize'],
 *   ];
 *
 * Ortam değişkenleri (TBA_API_KEY, DB_HOST, DB_NAME, DB_USER, DB_PASS) da desteklenir.
 * Bu dosya HTTP üzerinden erişilemez (api/.htaccess).
 */

$config = [
    'default_year' => 2026,

    // The Blue Alliance API v3
    'tba_api_key'  => getenv('TBA_API_KEY') ?: 'BURAYA_TBA_API_ANAHTARINIZI_YAZIN',
    'tba_base_url' => getenv('TBA_BASE_URL') ?: 'https://www.thebluealliance.com/api/v3',
    'http_timeout' => 12,          // saniye (cURL toplam süre)
    'cache_ttl'    => 600,         // saniye (10 dakika)
    'stale_ttl'    => 86400,       // TBA erişilemezse bu süreye kadar eski önbellek sunulur
    'cache_dir'    => dirname(__DIR__) . '/cache',

    // "country" alanı bu değerlerden biriyse etkinlik Türkiye etkinliği sayılır (küçük/büyük harf duyarsız)
    'allowed_countries' => ['Turkey', 'Türkiye', 'Turkiye'],

    // Scouting notları veritabanı (MySQL / MariaDB)
    'db' => [
        'host'    => getenv('DB_HOST') ?: 'localhost',
        'name'    => getenv('DB_NAME') ?: 'octo_replay',
        'user'    => getenv('DB_USER') ?: 'octo_user',
        'pass'    => getenv('DB_PASS') ?: 'degistir',
        'charset' => 'utf8mb4',
        // 'dsn'  => 'mysql:host=...;dbname=...;charset=utf8mb4',   // isteğe bağlı: tam DSN
    ],

    'notes' => [
        'max_length'        => 1000,   // not metni (karakter)
        'page_size'         => 100,    // tek istekte en fazla not
        'rate_limit_count'  => 8,      // pencere başına en fazla not (IP başına)
        'rate_limit_window' => 600,    // saniye
        'ip_salt'           => getenv('NOTES_IP_SALT') ?: 'octo-replay-degistir-bu-tuzu',
    ],
];

$local = __DIR__ . '/config.local.php';
if (is_file($local)) {
    $override = require $local;
    if (is_array($override)) {
        $config = array_replace_recursive($config, $override);
    }
}

return $config;
