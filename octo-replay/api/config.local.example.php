<?php
// Bu dosyayı config.local.php adıyla kopyalayıp doldurun. config.local.php .gitignore ile depoya yüklenmez.
return [
    'tba_api_key' => 'BURAYA_TBA_ANAHTARI',

    // Hosting panelinizden aldığınız MySQL bilgilerini doldurun, sonra schema.sql'i içe aktarın.
    'db' => [
        'host' => 'localhost',
        'name' => 'HESAP_octo',
        'user' => 'HESAP_octo',
        'pass' => 'VERITABANI_PAROLASI',
    ],

    // Rastgele uzun bir dize (kullanıcı IP'lerini hız sınırı için geri döndürülemez hash'lemede kullanılır)
    'notes' => ['ip_salt' => 'BURAYA-UZUN-RASTGELE-BIR-DIZE-YAZIN'],
];
