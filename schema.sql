-- Octo-Replay · scouting notları şeması (MySQL 5.7+ / MariaDB 10.3+)
-- Kullanım: phpMyAdmin > İçe Aktar veya `mysql -u KULLANICI -p VERITABANI < schema.sql`

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `scouting_notes` (
  `id`           INT UNSIGNED       NOT NULL AUTO_INCREMENT,
  `match_key`    VARCHAR(40)        NOT NULL COMMENT 'TBA maç anahtarı, ör. 2026tuis_qm12',
  `event_key`    VARCHAR(24)        NOT NULL COMMENT 'TBA etkinlik anahtarı, ör. 2026tuis',
  `team_number`  SMALLINT UNSIGNED  NULL     COMMENT 'Notun hedef takımı; NULL = genel maç notu',
  `author_team`  SMALLINT UNSIGNED  NOT NULL COMMENT 'Notu yazan takım numarası',
  `author_name`  VARCHAR(60)        NOT NULL DEFAULT '',
  `category`     ENUM('general','auto','cycle','defense','reliability','endgame') NOT NULL DEFAULT 'general',
  `rating`       TINYINT UNSIGNED   NULL     COMMENT '1-5 arası isteğe bağlı puan',
  `note`         TEXT               NOT NULL,
  `ip_hash`      CHAR(64)           NOT NULL COMMENT 'SHA-256(IP + tuz); hız sınırı için, geri çözülemez',
  `is_hidden`    TINYINT(1)         NOT NULL DEFAULT 0 COMMENT 'Moderasyon: 1 = gizli',
  `created_at`   DATETIME           NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_match_visible` (`match_key`, `is_hidden`, `id`),
  KEY `idx_team`          (`team_number`, `is_hidden`),
  KEY `idx_event`         (`event_key`),
  KEY `idx_rate_limit`    (`ip_hash`, `created_at`),
  CONSTRAINT `chk_rating` CHECK (`rating` IS NULL OR (`rating` BETWEEN 1 AND 5))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Moderasyon örneği (uygunsuz bir notu gizlemek için):
--   UPDATE scouting_notes SET is_hidden = 1 WHERE id = 123;
