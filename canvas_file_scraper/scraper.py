import os
import requests
from requests.exceptions import MissingSchema
import logging
import json
from tempfile import TemporaryFile
import urllib
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from canvasapi import Canvas
from canvasapi.exceptions import Unauthorized, ResourceDoesNotExist

from canvasapi.canvas_object import CanvasObject
from canvasapi.paginated_list import PaginatedList
from canvasapi.util import combine_kwargs


class MediaObject(CanvasObject):
    pass


def get_media_objects(self, *args, **kwargs):
    return PaginatedList(
        MediaObject,
        self._requester,
        "GET",
        "courses/{}/media_objects".format(self.id),
        {"course_id": self.id},
        _kwargs=combine_kwargs(**kwargs),
    )


class CanvasScraper:
    def __init__(
            self, base_url, api_key, path, overwrite,
            videos, markdown, logger=None):
        self.api_key = api_key
        self.base_url = self._create_base_url(base_url)
        self.headers = {'Authorization': f'Bearer {self.api_key}'}
        self._path = path
        self.overwrite = overwrite
        self.videos = videos
        self.markdown = markdown
        self._logger = logger
        self._canvas = Canvas(self.base_url, self.api_key)
        self.user = self._canvas.get_current_user()

        if not self._logger:
            self._logger = logging

        self._loggers = [self._logger]
        self._names = []
        self._ids = []

    def scrape(self):
        courses = self.user.get_courses()
        for c in courses:
            self.recurse_course(c)

    def recurse_course(self, course):
        try:
            self.push(course, "course")
        except KeyError:
            return

        try:
            fp_path = os.path.join(self.path, "front_page.html")
            fp_md_path = os.path.join(self.path, "front_page.md")
            fp = course.show_front_page().body

            if self.markdown and self._dl_page(fp, fp_path):
                self._markdownify(fp_path, fp_md_path)
        except (Unauthorized, ResourceDoesNotExist) as e:
            self.logger.warning(e)
            self.logger.warning(f"Front page not accesible")

        try:
            modules = course.get_modules()
            for m in modules:
                self.recurse_module(m)
        except (Unauthorized, ResourceDoesNotExist) as e:
            self.logger.warning(e)
            self.logger.warning(f"Modules not accesible")

        try:
            groups = course.get_groups()
            for g in groups:
                self.recurse_group(g)
        except (Unauthorized, ResourceDoesNotExist) as e:
            self.logger.warning(e)
            self.logger.warning(f"Groups not accesible")
        self.scrape_files(course)

        self.scrape_media(course)


        self.pop()

    def recurse_group(self, group):
        try:
            self.push(group, "group")
        except KeyError:
            return
        json_path = os.path.join(self.path, "group.json")
        self._dl_obj(group, json_path)
        self.scrape_files(group)
        self.pop()

    def scrape_files(self, obj):
        # Hack to put files under a separate subfolder from modules
        self.push_raw(f"files_{obj.id}", "files", 0)
        try:
            # get_folders() returns a flat list of all folders
            folders = obj.get_folders()
            for f in folders:
                self.recurse_folder(f)
        except Unauthorized:
            self.logger.warning(f"Files not accesible")
        self.pop()

    def scrape_media(self, obj):
        # Hack to put media under a separate subfolder from modules
        self.push_raw(f"media_{obj.id}", "media", 0)
        try:
            obj.__class__.get_media_objects = get_media_objects
            media_objs = obj.get_media_objects()
            for m in media_objs:
                if "video" in m.media_type:
                    self.handle_media_video(m)
                else:
                    self.logger.warning(
                        f"Media '{m.title}' type {m.media_type} is unsupported")
                    import pdb
                    pdb.set_trace()
        except (Unauthorized, ResourceDoesNotExist) as e:
            self.logger.warning(e)
            self.logger.warning(f"Media objects not accesible")
        self.pop()

    def recurse_folder(self, folder):
        self.push(folder, "folder", name_key="full_name")
        files = folder.get_files()
        try:
            for f in files:
                try:
                    f_name = f.title
                except AttributeError:
                    try:
                        f_name = f.display_name
                    except Exception as e:
                        import pdb
                        pdb.set_trace()

                f_path = os.path.join(self.path, f_name)

                if self._should_write(f_path):
                    self.logger.info(f"Downloading {f_path}")
                    f.download(f_path)
                    self.logger.info(f"{f_path} downloaded")
        except Unauthorized:
            self.logger.warning(f"folder not accesible")
        self.pop()

    def recurse_module(self, module):
        self.push(module, "module")
        items = module.get_module_items()
        for i in items:
            self.recurse_item(i)
        self.pop()

    def recurse_item(self, item):
        self.push(item, "item", name_key="title")
        if item.type == "File":
            self.handle_file(item)
        elif item.type == "Page":
            self.handle_page(item)
        elif item.type == "Assignment":
            self.handle_assignment(item)
        elif item.type == "Quiz":
            self.handle_quiz(item)
        else:
            self.logger.warning(f"Unsupported type {item.type}")
            import pdb
            pdb.set_trace()
        self.pop()

    def handle_file(self, item):
        file_name = item.title
        file_url = item.url
        file_path = os.path.join(self.path, file_name)
        self._dl(file_url, file_path)

    def handle_media_video(self, item):
        media_name = item.title
        media_path = os.path.join(self.path, media_name)
        sources = item.media_sources
        sources.sort(key=lambda s: int(s['size']), reverse=True)
        media_url = sources[0]['url']
        self._dl(media_url, media_path)

    def handle_page(self, item):
        page = self._canvas.get_course(
            item.course_id).get_page(item.page_url).body

        page_path = os.path.join(self.path, "page.html")
        page_md_path = os.path.join(self.path, "page.md")

        if self.markdown and self._dl_page(page, page_path):
            self._markdownify(page_path, page_md_path)
            self._dl_page_data(page_path)

    def handle_assignment(self, item):
        page_path = os.path.join(self.path, "assignment.html")
        page_md_path = os.path.join(self.path, "assignment.md")
        json_path = os.path.join(self.path, "assignment.json")
        assignment = self._canvas.get_course(
            item.course_id).get_assignment(item.content_id)

        self._dl_obj(assignment, json_path)

        page = assignment.description
        if page:
            if self.markdown and self._dl_page(page, page_path):
                self._markdownify(page_path, page_md_path)
                self._dl_page_data(page_path)

        submission = assignment.get_submission(self.user)
        self.handle_submission(submission)

    def handle_quiz(self, item):
        page_path = os.path.join(self.path, "quiz.html")
        page_md_path = os.path.join(self.path, "quiz.md")
        json_path = os.path.join(self.path, "quiz.json")
        quiz = self._canvas.get_course(
            item.course_id).get_quiz(item.content_id)
        page = quiz.description
        if page:
            if self.markdown and self._dl_page(page, page_path):
                self._markdownify(page_path, page_md_path)
                self._dl_page_data(page_path)
        self._dl_obj(quiz, json_path)

    def handle_submission(self, submission):
        self.push(submission, "submission", name_key="id")
        json_path = os.path.join(self.path, f"submission_{submission.id}.json")

        try:
            attachments = submission.attachments
            for a in attachments:
                f_path = os.path.join(self.path, a["filename"])
                url = a["url"]
                self._dl(url, f_path)
        except AttributeError:
            self.logger.warning("No attachments found")

        self._dl_obj(submission, json_path)
        self.pop()

    def push(self, obj, type, name_key="name"):
        id = obj.id
        try:
            name = str(getattr(obj, name_key))
        except:
            name = str(id)

        self.push_raw(name, type, id)

    def push_raw(self, name, type, id):
        self._push_logger(f"{type}_{id}")
        self._push_name(name)
        self._push_id(id)
        self.logger.info(name)

    def pop(self):
        self._pop_logger()
        self._pop_name()
        self._pop_id()

    def get_all_objects(self, url):
        self.logger.debug(f"Grabbing all pages for {url}")
        objects = []
        page = 1
        while True:
            r = self._get(url, params={"page": page})
            if not r.json():
                break
            objects.extend(r.json())
            self.logger.debug(f"Grabbed page {page}")
            page += 1
        return objects

    @property
    def logger(self):
        return self._loggers[-1]

    @property
    def path(self):
        return os.path.join(self._path, *self._names)

    @property
    def name(self):
        return self._names[-1]

    @property
    def id(self):
        return self._ids[-1]

    @staticmethod
    def _create_base_url(base_url):
        if "https" not in base_url:
            base_url = f"https://{base_url}"
        return base_url

    def _courses_url(self):
        return f"{self.base_url}/courses"

    def _course_url(self, course_id):
        return f"{self._courses_url()}/{course_id}"

    def _course_frontpage_url(self, course_id):
        return f"{self._course_url(course_id)}/front_page"

    def _modules_url(self, course_id):
        return f"{self._course_url(course_id)}/modules"

    def _kaltura_manifest_url(self, base_url, entry_id, flavor_id):
        base_url = base_url[:base_url.index("embedIframeJs")]
        return os.path.join(
            base_url,
            "playManifest/entryId",
            str(entry_id),
            "flavorIds",
            str(flavor_id),
            "format/applehttp/protocol/https/a.m3u8")

    def _get(self, url, params=None):
        return requests.get(url, params=params, headers=self.headers)

    def _mkd(self, path):
        return os.makedirs(path, exist_ok=True)

    def _dl(self, url, path):
        if self._should_write(path):
            try:
                self.logger.info(f"Downloading {path}")
                r = self._get(url)
                with open(path, "wb") as f:
                    f.write(r.content)
                    self.logger.info(f"{path} downloaded")
                    return True
            except MissingSchema as e:
                self.logger.error(f"{url} is not a valid url")
                return False
            except Exception as e:
                self.logger.error("file download failed")
                import pdb
                pdb.set_trace()
                self.logger.error(e)

    def _dl_page(self, page, path):
        if self._should_write(path):
            with open(path, "w") as f:
                f.writelines(page)
                self.logger.info(f"{path} downloaded")
                return True

    def _dl_obj(self, obj, path):
        if self._should_write(path):
            with open(path, "w") as f:
                json.dump(obj.__dict__, f, indent=2, default=str)
                self.logger.info(f"{path} downloaded")

    def _dl_page_data(self, src_path):
        self.logger.info(f"Downloading page data for {src_path}")
        with open(src_path, "r") as f:
            src = f.read()

        soup = BeautifulSoup(src, "html.parser")
        links = soup.find_all('a', **{'data-api-returntype': 'File'})
        for link in links:
            dl_path = os.path.join(self.path, "files", link["title"])
            self._dl(link["href"], dl_path)

        if self.videos:
            # Download Kaltura videos
            videos = soup.find_all('iframe', **{'id': 'kaltura_player'})
            for idx, video in enumerate(videos):
                video_path = os.path.join(self.path, "videos", f"{idx}.mp4")
                self._dl_video(video["src"], video_path)

    def _dl_video(self, base_url, path):
        if not self._should_write(path):
            return
        # Get data from Kaltura iframe
        lines = requests.get(base_url).text.splitlines()
        iframe_data = next(
            (l for l in lines if "kalturaIframePackageData" in l), None)
        if not iframe_data:
            self.logger.warning(f"iframe data not found for {base_url}")
            return
        # Ignore js syntax, pull json text out of line
        iframe_data = iframe_data[iframe_data.index("{"):-1]
        iframe_data = json.loads(iframe_data)
        try:
            flavor_assets = (iframe_data["entryResult"]
                                        ["contextData"]
                                        ["flavorAssets"])
        except KeyError:
            self.logger.warning(f"flavorAssets not found in {base_url}")
            return

        flavor_asset = next(
            (f for f in flavor_assets if f.get("flavorParamsId") == 5),
            None)
        if not flavor_asset:
            self.logger.warning(
                f"Could not find correct flavorAsset for {base_url}")
            return
        try:
            entry_id = flavor_asset["entryId"]
            flavor_id = flavor_asset["id"]
        except KeyError:
            self.logger.warning(
                f"Could not find keys inside flavorAsset for {base_url}")
            return
        manifest_url = self._kaltura_manifest_url(
            base_url, entry_id, flavor_id)
        lines = requests.get(manifest_url).text.splitlines()
        index_url = next((l for l in lines if "index" in l), None)
        if not index_url:
            self.logger.warning(
                f"Could not find index urlfor {base_url}")
            return
        index = filter(
            lambda l: not l.startswith("#"),
            requests.get(index_url).text.splitlines())
        streaming_url = index_url.replace("index.m3u8", "")
        with TemporaryFile() as tf:
            for i in index:
                self.logger.info(f"Downloading video segment {i}")
                segment_url = os.path.join(streaming_url, i)
                tf.write(requests.get(segment_url).content)
            with open(path, "wb") as f:
                tf.seek(0)
                f.write(tf.read())
            self.logger.info(f"Downloaded {path} successfully")

    def _markdownify(self, src_path, dest_path):
        if self._should_write(dest_path):
            self.logger.info(f"Converting {src_path} to markdown")
            with open(src_path, "r") as f:
                src = f.read()
            with open(dest_path, "w") as f:
                f.writelines(md(src))

    def _should_write(self, path):
        if os.path.isfile(path) and self.overwrite is "no":
            self.logger.debug(f"Skipping file {path}")
            return False
        elif (self.overwrite is "ask" and
                input(f"{path} already exists, overwrite? (y/n)") != "y"):
            return False
        # Ensure folder exists before writing
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return True

    def _push_logger(self, name):
        self._loggers.append(self.logger.getChild(name))

    def _pop_logger(self):
        self._loggers.pop(-1)

    def _push_name(self, name):
        self._names.append(name)
        self._mkd(self.path)

    def _pop_name(self):
        self._names.pop(-1)

    def _push_id(self, id):
        self._ids.append(id)

    def _pop_id(self):
        self._ids.pop(-1)




