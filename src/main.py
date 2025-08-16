import argparse
import datetime
import json
import logging
import os
import time
from typing import List, Optional

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from yandex_music import Client as YandexClient

from base_class import MusicService
from database_manager import DatabaseManager

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def check_and_fix_spotify_cache():
    """Проверяет и исправляет кэш файл Spotify если он некорректный"""
    cache_path = "./.cache"
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                content = f.read().strip()
                
            # Проверяем, является ли это валидным JSON
            json.loads(content)
            logger.info("Кэш файл Spotify корректен")
            
        except json.JSONDecodeError:
            logger.warning("Обнаружен некорректный кэш файл Spotify, попытка исправления...")
            try:
                # Пытаемся исправить, если это Python dict строка
                if content.startswith("{'") and content.endswith("'}"):
                    # Преобразуем Python dict строку в валидный JSON
                    content_fixed = content.replace("'", '"')
                    # Проверяем, что теперь это валидный JSON
                    parsed = json.loads(content_fixed)
                    
                    # Перезаписываем файл с корректным JSON
                    with open(cache_path, "w") as f:
                        json.dump(parsed, f)
                    logger.info("Кэш файл Spotify успешно исправлен")
                else:
                    logger.error("Не удалось исправить кэш файл автоматически. Удаляем некорректный файл.")
                    os.remove(cache_path)
            except Exception as e:
                logger.error(f"Ошибка при исправлении кэш файла: {e}")
                logger.info("Удаляем некорректный кэш файл")
                try:
                    os.remove(cache_path)
                except:
                    pass
        except Exception as e:
            logger.error(f"Ошибка при проверке кэш файла: {e}")
    else:
        logger.warning("Кэш файл Spotify не найден. Убедитесь, что вы выполнили аутентификацию.")


class YandexMusic(MusicService):
    def __init__(self, db_manager: DatabaseManager, token: str):
        super().__init__(db_manager)
        self.client = YandexClient(token=token).init()

    def get_tracks(self, force_full_sync: bool) -> List[dict]:
        short_tracks = self.client.users_likes_tracks()
        full_tracks = []

        last_sync = (
            self.db_manager.get_last_sync_time("yandex")
            if not force_full_sync
            else None
        )
        if last_sync:
            last_sync = last_sync.replace(tzinfo=datetime.timezone.utc)

        for track in short_tracks:
            added_at = datetime.datetime.strptime(
                track.timestamp, "%Y-%m-%dT%H:%M:%S%z"
            ) + datetime.timedelta(hours=3)
            if force_full_sync or not self.db_manager.check_track_exists(
                "yandex", track.id
            ):
                if last_sync is None or added_at > last_sync:
                    full_tracks.append(track.fetch_track())

        return full_tracks

    def search_track(self, artist: str, title: str) -> Optional[dict]:
        query = f"{artist} {title}"
        result = self.client.search(query)
        if result["best"] and result["best"]["type"] == "track":
            return result["best"]["result"]
        return None

    def add_track(self, track: dict) -> Optional[str]:
        yandex_track = self.search_track(
            track["track"]["artists"][0]["name"], track["track"]["name"]
        )
        if yandex_track:
            self.client.users_likes_tracks_add(yandex_track["id"])
            return yandex_track["id"]
        logger.warning(
            f"Track not found in Yandex: {track['track']['artists'][0]['name']} - {track['track']['name']}"
        )
        self.db_manager.add_undiscovered_track(
            "yandex", track["track"]["artists"][0]["name"], track["track"]["name"]
        )
        return None

    def remove_duplicates(self):
        tracks = self.client.users_likes_tracks()
        tracks_seen = set()
        tracks_to_remove = []

        for track in tracks:
            full_track = track.fetch_track()
            track_key = (full_track.title.lower(), full_track.artists[0].name.lower())

            if track_key in tracks_seen:
                tracks_to_remove.append(track.id)
            else:
                tracks_seen.add(track_key)

        if tracks_to_remove:
            self.client.users_likes_tracks_remove(tracks_to_remove)
            logger.info(f"Removed {len(tracks_to_remove)} duplicate tracks from Yandex")


class SpotifyMusic(MusicService):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__(db_manager)
        self.client = spotipy.Spotify(
            auth_manager=SpotifyOAuth(scope="user-library-read user-library-modify")
        )

    def get_tracks(self, force_full_sync: bool) -> List[dict]:
        last_sync = (
            self.db_manager.get_last_sync_time("spotify")
            if not force_full_sync
            else None
        )
        if last_sync:
            last_sync = last_sync.replace(tzinfo=datetime.timezone.utc)

        results = self.client.current_user_saved_tracks()
        tracks = []

        while results["items"]:
            for item in results["items"]:
                track = item["track"]
                added_at = datetime.datetime.strptime(
                    item["added_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=3)
                if force_full_sync or last_sync is None or added_at > last_sync:
                    if force_full_sync or not self.db_manager.check_track_exists(
                        "spotify", track["id"]
                    ):
                        tracks.append(item)

            if results["next"] and (
                force_full_sync
                or not last_sync
                or any(added_at > last_sync for item in results["items"])
            ):
                results = self.client.next(results)
            else:
                break

        return tracks

    def search_track(self, artist: str, title: str) -> Optional[dict]:
        query = f"{artist} {title}"
        results = self.client.search(q=query, type="track", limit=1)
        if results["tracks"]["items"]:
            return results["tracks"]["items"][0]
        return None

    def add_track(self, track: dict) -> Optional[str]:
        if not self._check_duplicate(track.artists[0].name, track.title):
            spotify_track = self.search_track(track.artists[0].name, track.title)
            if spotify_track:
                self.client.current_user_saved_tracks_add([spotify_track["id"]])
                return spotify_track["id"]
            else:
                logger.warning(
                    f"Track not found in Spotify: {track.artists[0].name} - {track.title}"
                )
                self.db_manager.add_undiscovered_track(
                    "spotify", track.artists[0].name, track.title
                )
        else:
            logger.info(
                f"Duplicate found in Spotify: {track.artists[0].name} - {track.title}"
            )
        return None

    def _check_duplicate(self, artist: str, title: str) -> bool:
        results = self.client.search(
            q=f"track:{title} artist:{artist}", type="track", limit=50
        )
        for item in results["tracks"]["items"]:
            if (
                item["name"].lower() == title.lower()
                and item["artists"][0]["name"].lower() == artist.lower()
            ):
                if self.client.current_user_saved_tracks_contains([item["id"]])[0]:
                    return True
        return False

    def remove_duplicates(self):
        offset = 0
        limit = 50
        tracks_seen = set()
        tracks_to_remove = []

        while True:
            results = self.client.current_user_saved_tracks(limit=limit, offset=offset)
            if len(results["items"]) == 0:
                break

            for item in results["items"]:
                track = item["track"]
                track_key = (track["name"].lower(), track["artists"][0]["name"].lower())

                if track_key in tracks_seen:
                    tracks_to_remove.append(track["id"])
                else:
                    tracks_seen.add(track_key)

            offset += limit

        if tracks_to_remove:
            for i in range(0, len(tracks_to_remove), 50):
                batch = tracks_to_remove[i : i + 50]
                self.client.current_user_saved_tracks_delete(batch)
                logger.info(f"Removed {len(batch)} duplicate tracks from Spotify")


class MusicSynchronizer:
    def __init__(
        self,
        yandex_service: YandexMusic,
        spotify_service: SpotifyMusic,
        db_manager: DatabaseManager,
    ):
        self.yandex = yandex_service
        self.spotify = spotify_service
        self.db_manager = db_manager

    def sync_tracks(self, force_full_sync: bool = False):
        current_time = datetime.datetime.now()
        # Sync Yandex to Spotify
        yandex_tracks = self.yandex.get_tracks(force_full_sync)
        for track in yandex_tracks:
            spotify_id = self.spotify.add_track(track)
            if spotify_id:
                self.db_manager.insert_or_update_track(
                    track.id, spotify_id, track.artists[0].name, track.title
                )
                logger.info(
                    f"Added to Spotify: {track.artists[0].name} - {track.title}"
                )

        self.db_manager.update_last_sync_time("yandex", current_time)

        # Sync Spotify to Yandex
        spotify_tracks = self.spotify.get_tracks(force_full_sync)
        for item in spotify_tracks:
            track = item["track"]
            yandex_id = self.yandex.add_track(item)
            if yandex_id:
                self.db_manager.insert_or_update_track(
                    yandex_id, track["id"], track["artists"][0]["name"], track["name"]
                )
                logger.info(
                    f"Added to Yandex: {track['artists'][0]['name']} - {track['name']}"
                )

        self.db_manager.update_last_sync_time("spotify", current_time)

    def remove_duplicates(self):
        self.spotify.remove_duplicates()
        self.yandex.remove_duplicates()


def parse_arguments():
    parser = argparse.ArgumentParser(description="Music Synchronizer")
    parser.add_argument(
        "--sleep",
        type=int,
        default=60,
        help="Time to sleep between syncs in seconds (default: 60)",
    )
    parser.add_argument(
        "--force-full-sync",
        action="store_true",
        help="Force a full sync of all tracks",
    )
    parser.add_argument(
        "--remove-duplicates",
        action="store_true",
        help="Remove duplicate tracks after the first sync",
    )
    return parser.parse_args()

def main():
    args = parse_arguments()
    logger.info(f"Запущен скрипт с параметрами: {args}")
    load_dotenv()
    
    # Проверяем и исправляем кэш файл Spotify перед началом работы
    check_and_fix_spotify_cache()
    
    yandex_token = os.getenv("YANDEX_TOKEN")
    if not yandex_token:
        logger.error("YANDEX_TOKEN не найден в файле .env")
        return

    # Параметры подключения к PostgreSQL
    db_params = {
        "dbname": os.getenv("POSTGRES_DB", "music_sync"),
        "user": os.getenv("POSTGRES_USER"),
        "password": os.getenv("POSTGRES_PASSWORD"),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432")
    }

    db_manager = DatabaseManager(db_params)
    yandex_service = YandexMusic(db_manager, yandex_token)
    spotify_service = SpotifyMusic(db_manager)
    synchronizer = MusicSynchronizer(yandex_service, spotify_service, db_manager)

    try:
        first_run = True
        while True:
            try:
                logger.info("Синхронизация треков...")
                synchronizer.sync_tracks(force_full_sync=args.force_full_sync)
                
                if first_run and args.remove_duplicates:
                    logger.info("Удаление дубликатов...")
                    synchronizer.remove_duplicates()
                    first_run = False
                
                logger.info(f"Ожидание {args.sleep} секунд...")
                time.sleep(args.sleep)
            except Exception as e:
                logger.error(f"Произошла ошибка: {e}")
                logger.info("Ожидание 60 секунд перед повторной попыткой...")
                time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Процесс синхронизации прерван пользователем")
    finally:
        db_manager.close()


if __name__ == "__main__":
    main()
