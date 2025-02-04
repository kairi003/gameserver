
DROP TABLE IF EXISTS `room_member`;
DROP TABLE IF EXISTS `user`;
CREATE TABLE `user` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `name` varchar(255) DEFAULT NULL,
  `token` varchar(255) DEFAULT NULL,
  `leader_card_id` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY (`token`)
);
DROP TABLE IF EXISTS `room`;
CREATE TABLE `room` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `live_id` int NOT NULL,
  `joined_user_count` int NOT NULL DEFAULT 0,
  `max_user_count` int NOT NULL DEFAULT 0,
  `wait_room_status` int DEFAULT NULL,
  PRIMARY KEY (`id`)
);
CREATE TABLE `room_member` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` bigint NOT NULL,
  `room_id` bigint NOT NULL,
  `select_difficulty` int DEFAULT NULL,
  `is_host` boolean NOT NULL DEFAULT false,
  `judge_count_list` json DEFAULT NULL,
  `score` int DEFAULT NULL,
  `ttl` datetime NOT NULL DEFAULT (ADDTIME(NOW(), 10)),
  FOREIGN KEY (`user_id`) REFERENCES `user` (`id`),
  FOREIGN KEY (`room_id`) REFERENCES `room` (`id`),
  PRIMARY KEY (`id`),
  UNIQUE KEY (`user_id`)
);