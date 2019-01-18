import datetime
import os
import uuid
from typing import Any, Dict, List

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.views import serve
from django.db.models import F
from django.http import Http404, HttpResponse
from django.utils.timezone import now
from django.views import static
from django.views.generic.base import View
from mypy_extensions import TypedDict

from .. import __license__ as license, __url__ as url, __version__ as version
from ..utils import views as utils_views
from ..utils.arguments import arguments
from ..utils.auth import GROUP_ADMIN_PK, anonymous_is_enabled, has_perm, in_some_groups
from ..utils.autoupdate import inform_changed_data, inform_deleted_data
from ..utils.plugins import (
    get_plugin_description,
    get_plugin_license,
    get_plugin_url,
    get_plugin_verbose_name,
    get_plugin_version,
)
from ..utils.rest_api import (
    GenericViewSet,
    ListModelMixin,
    ModelViewSet,
    Response,
    RetrieveModelMixin,
    ValidationError,
    detail_route,
    list_route,
)
from .access_permissions import (
    ChatMessageAccessPermissions,
    ConfigAccessPermissions,
    CountdownAccessPermissions,
    HistoryAccessPermissions,
    ProjectorAccessPermissions,
    ProjectorMessageAccessPermissions,
    TagAccessPermissions,
)
from .config import config
from .exceptions import ConfigError, ConfigNotFound
from .models import (
    ChatMessage,
    ConfigStore,
    Countdown,
    History,
    HistoryData,
    ProjectionDefault,
    Projector,
    ProjectorMessage,
    Tag,
)


# Special Django views


class IndexView(View):
    """
    The primary view for the OpenSlides client. Serves static files. If a file
    does not exist or a directory is requested, the index.html is delivered instead.
    """

    cache: Dict[str, str] = {}
    """
    Saves the path to the index.html.

    May be extended later to cache every template.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        no_caching = arguments.get("no_template_caching", False)
        if "index" not in self.cache or no_caching:
            self.cache["index"] = finders.find("index.html")

        self.index_document_root, self.index_path = os.path.split(self.cache["index"])

    def get(self, request, path, **kwargs) -> HttpResponse:
        """
        Tries to serve the requested file. If it is not found or a directory is
        requested, the index.html is delivered.
        """
        try:
            response = serve(request, path, **kwargs)
        except Http404:
            response = static.serve(
                request,
                self.index_path,
                document_root=self.index_document_root,
                **kwargs,
            )
        return response


# Viewsets for the REST API


class ProjectorViewSet(ModelViewSet):
    """
    API endpoint for the projector slide info.

    There are the following views: See strings in check_view_permissions().
    """

    access_permissions = ProjectorAccessPermissions()
    queryset = Projector.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action == "metadata":
            result = has_perm(self.request.user, "core.can_see_projector")
        elif self.action in (
            "create",
            "update",
            "partial_update",
            "destroy",
            "activate_elements",
            "prune_elements",
            "update_elements",
            "deactivate_elements",
            "clear_elements",
            "project",
            "control_view",
            "set_resolution",
            "set_scroll",
            "control_blank",
            "broadcast",
            "set_projectiondefault",
        ):
            result = has_perm(self.request.user, "core.can_see_projector") and has_perm(
                self.request.user, "core.can_manage_projector"
            )
        else:
            result = False
        return result

    def destroy(self, *args, **kwargs):
        """
        REST API operation for DELETE requests.

        Assigns all ProjectionDefault objects from this projector to the
        default projector (pk=1). Resets broadcast if set to this projector.
        """
        projector_instance = self.get_object()
        for projection_default in ProjectionDefault.objects.all():
            if projection_default.projector.id == projector_instance.id:
                projection_default.projector_id = 1
                projection_default.save()
        if config["projector_broadcast"] == projector_instance.pk:
            config["projector_broadcast"] = 0
        return super(ProjectorViewSet, self).destroy(*args, **kwargs)

    @detail_route(methods=["post"])
    def activate_elements(self, request, pk):
        """
        REST API operation to activate projector elements. It expects a POST
        request to /rest/core/projector/<pk>/activate_elements/ with a list
        of dictionaries to be appended to the projector config entry.
        """
        if not isinstance(request.data, list):
            raise ValidationError({"detail": "Data must be a list."})

        projector_instance = self.get_object()
        projector_config = projector_instance.config
        for element in request.data:
            if element.get("name") is None:
                raise ValidationError(
                    {"detail": "Invalid projector element. Name is missing."}
                )
            projector_config[uuid.uuid4().hex] = element

        serializer = self.get_serializer(
            projector_instance, data={"config": projector_config}, partial=False
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @detail_route(methods=["post"])
    def prune_elements(self, request, pk):
        """
        REST API operation to activate projector elements. It expects a POST
        request to /rest/core/projector/<pk>/prune_elements/ with a list of
        dictionaries to write them to the projector config entry. All old
        entries are deleted but not entries with stable == True.
        """
        if not isinstance(request.data, list):
            raise ValidationError({"detail": "Data must be a list."})

        projector = self.get_object()
        elements = request.data
        if not isinstance(elements, list):
            raise ValidationError({"detail": "The data has to be a list."})
        for element in elements:
            if not isinstance(element, dict):
                raise ValidationError({"detail": "All elements have to be dicts."})
            if element.get("name") is None:
                raise ValidationError(
                    {"detail": "Invalid projector element. Name is missing."}
                )
        return Response(self.prune(projector, elements))

    def prune(self, projector, elements):
        """
        Prunes all non stable elements from the projector and adds the given elements.
        The elements have to a list of dicts, each gict containing at least a name. This
        is not validated at this point! Should be done before.
        Returns the new serialized data.
        """
        projector_config = {}
        for key, value in projector.config.items():
            if value.get("stable"):
                projector_config[key] = value
        for element in elements:
            projector_config[uuid.uuid4().hex] = element

        serializer = self.get_serializer(
            projector, data={"config": projector_config}, partial=False
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # reset scroll level
        if projector.scroll != 0:
            projector.scroll = 0
            projector.save()
        return serializer.data

    @detail_route(methods=["post"])
    def update_elements(self, request, pk):
        """
        REST API operation to update projector elements. It expects a POST
        request to /rest/core/projector/<pk>/update_elements/ with a
        dictonary to update the projector config. This must be a dictionary
        with UUIDs as keys and projector element dictionaries as values.

        Example:

        {
            "191c0878cdc04abfbd64f3177a21891a": {
                "name": "core/countdown",
                "stable": true,
                "status": "running",
                "countdown_time": 1374321600.0,
                "visable": true,
                "default": 42
            }
        }
        """
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Data must be a dictionary."})
        error = {
            "detail": "Data must be a dictionary with UUIDs as keys and dictionaries as values."
        }
        for key, value in request.data.items():
            try:
                uuid.UUID(hex=str(key))
            except ValueError:
                raise ValidationError(error)
            if not isinstance(value, dict):
                raise ValidationError(error)

        projector_instance = self.get_object()
        projector_config = projector_instance.config
        for key, value in request.data.items():
            if key not in projector_config:
                raise ValidationError(
                    {"detail": "Invalid projector element. Wrong UUID."}
                )
            projector_config[key].update(request.data[key])

        serializer = self.get_serializer(
            projector_instance, data={"config": projector_config}, partial=False
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @detail_route(methods=["post"])
    def deactivate_elements(self, request, pk):
        """
        REST API operation to deactivate projector elements. It expects a
        POST request to /rest/core/projector/<pk>/deactivate_elements/ with
        a list of hex UUIDs. These are the projector_elements in the config
        that should be deleted.
        """
        if not isinstance(request.data, list):
            raise ValidationError({"detail": "Data must be a list of hex UUIDs."})
        for item in request.data:
            try:
                uuid.UUID(hex=str(item))
            except ValueError:
                raise ValidationError({"detail": "Data must be a list of hex UUIDs."})

        projector_instance = self.get_object()
        projector_config = projector_instance.config
        for key in request.data:
            try:
                del projector_config[key]
            except KeyError:
                raise ValidationError({"detail": "Invalid UUID."})

        serializer = self.get_serializer(
            projector_instance, data={"config": projector_config}, partial=False
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @detail_route(methods=["post"])
    def clear_elements(self, request, pk):
        """
        REST API operation to deactivate all projector elements but not
        entries with stable == True. It expects a POST request to
        /rest/core/projector/<pk>/clear_elements/.
        """
        projector = self.get_object()
        return Response(self.clear(projector))

    def clear(self, projector):
        projector_config = {}
        for key, value in projector.config.items():
            if value.get("stable"):
                projector_config[key] = value

        serializer = self.get_serializer(
            projector, data={"config": projector_config}, partial=False
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return serializer.data

    @list_route(methods=["post"])
    def project(self, request, *args, **kwargs):
        """
        REST API operation. Does a combination of clear_elements and prune_elements:
        In the most cases when projecting an element it first need to be removed from
        all projectors where it is projected. In a second step the new element (which
        may be not given if the element is just deprojected) needs to be projected on
        a maybe different projector. The request data has to have this scheme:
        {
            clear_ids: [<projector id1>, ...],      # May be an empty list
            prune: {                                # May not be given.
                id: <projector id>,
                element: <projector element to add>
            }
        }
        """
        # The data has to be a dict.
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "The data has to be a dict."})

        # Get projector ids to clear
        clear_projector_ids = request.data.get("clear_ids", [])
        for id in clear_projector_ids:
            if not isinstance(id, int):
                raise ValidationError({"detail": f'The id "{id}" has to be int.'})

        # Get the projector id and validate element to prune. This is optional.
        prune = request.data.get("prune")
        if prune is not None:
            if not isinstance(prune, dict):
                raise ValidationError({"detail": "Prune has to be an object."})
            prune_projector_id = prune.get("id")
            if not isinstance(prune_projector_id, int):
                raise ValidationError(
                    {"detail": "The prune projector id has to be int."}
                )

            # Get the projector after all clear operations, but check, if it exist.
            if not Projector.objects.filter(pk=prune_projector_id).exists():
                raise ValidationError(
                    {
                        "detail": f'The projector with id "{prune_projector_id}" does not exist'
                    }
                )

            prune_element = prune.get("element", {})
            if not isinstance(prune_element, dict):
                raise ValidationError(
                    {"detail": "Prune element has to be a dict or not given."}
                )
            if prune_element.get("name") is None:
                raise ValidationError(
                    {"detail": "Invalid projector element. Name is missing."}
                )

        # First step: Clear all given projectors
        for projector in Projector.objects.filter(pk__in=clear_projector_ids):
            self.clear(projector)

        # Second step: optionally prune
        if prune is not None:
            # This get is save. We checked that the projector exists above.
            prune_projector = Projector.objects.get(pk=prune_projector_id)
            self.prune(prune_projector, [prune_element])

        return Response()

    @detail_route(methods=["post"])
    def set_resolution(self, request, pk):
        """
        REST API operation to set the resolution.

        It is actually unused, because the resolution is currently set in the config.
        But with the multiprojector feature this will become importent to set the
        resolution per projector individually.

        It expects a POST request to
        /rest/core/projector/<pk>/set_resolution/ with a dictionary with the width
        and height and the values.

        Example:

        {
            "width": "1024",
            "height": "768"
        }
        """
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Data must be a dictionary."})
        if request.data.get("width") is None or request.data.get("height") is None:
            raise ValidationError({"detail": "A width and a height have to be given."})
        if not isinstance(request.data["width"], int) or not isinstance(
            request.data["height"], int
        ):
            raise ValidationError({"detail": "Data has to be integers."})
        if (
            request.data["width"] < 800
            or request.data["width"] > 3840
            or request.data["height"] < 340
            or request.data["height"] > 2880
        ):
            raise ValidationError(
                {"detail": "The Resolution have to be between 800x340 and 3840x2880."}
            )

        projector_instance = self.get_object()
        projector_instance.width = request.data["width"]
        projector_instance.height = request.data["height"]
        projector_instance.save()

        message = f"Changing resolution to {request.data['width']}x{request.data['height']} was successful."
        return Response({"detail": message})

    @detail_route(methods=["post"])
    def control_view(self, request, pk):
        """
        REST API operation to control the projector view, i. e. scale and
        scroll the projector.

        It expects a POST request to
        /rest/core/projector/<pk>/control_view/ with a dictionary with an
        action ('scale' or 'scroll') and a direction ('up', 'down' or
        'reset').

        Example:

        {
            "action": "scale",
            "direction": "up"
        }
        """
        if not isinstance(request.data, dict):
            raise ValidationError({"detail": "Data must be a dictionary."})
        if request.data.get("action") not in ("scale", "scroll") or request.data.get(
            "direction"
        ) not in ("up", "down", "reset"):
            raise ValidationError(
                {
                    "detail": "Data must be a dictionary with an action ('scale' or 'scroll') "
                    "and a direction ('up', 'down' or 'reset')."
                }
            )

        projector_instance = self.get_object()
        if request.data["action"] == "scale":
            if request.data["direction"] == "up":
                projector_instance.scale = F("scale") + 1
            elif request.data["direction"] == "down":
                projector_instance.scale = F("scale") - 1
            else:
                # request.data['direction'] == 'reset'
                projector_instance.scale = 0
        else:
            # request.data['action'] == 'scroll'
            if request.data["direction"] == "up":
                projector_instance.scroll = F("scroll") + 1
            elif request.data["direction"] == "down":
                projector_instance.scroll = F("scroll") - 1
            else:
                # request.data['direction'] == 'reset'
                projector_instance.scroll = 0

        projector_instance.save(skip_autoupdate=True)
        projector_instance.refresh_from_db()
        inform_changed_data(projector_instance)
        action = (request.data["action"].capitalize(),)
        direction = (request.data["direction"],)
        message = f"{action} {direction} was successful."
        return Response({"detail": message})

    @detail_route(methods=["post"])
    def set_scroll(self, request, pk):
        """
        REST API operation to scroll the projector.

        It expects a POST request to
        /rest/core/projector/<pk>/set_scroll/ with a new value for scroll.
        """
        if not isinstance(request.data, int):
            raise ValidationError({"detail": "Data must be an int."})

        projector_instance = self.get_object()
        projector_instance.scroll = request.data

        projector_instance.save()
        message = f"Setting scroll to {request.data} was successful."
        return Response({"detail": message})

    @detail_route(methods=["post"])
    def control_blank(self, request, pk):
        """
        REST API operation to blank the projector.

        It expects a POST request to
        /rest/core/projector/<pk>/control_blank/ with a value for blank.
        """
        if not isinstance(request.data, bool):
            raise ValidationError({"detail": "Data must be a bool."})

        projector_instance = self.get_object()
        projector_instance.blank = request.data
        projector_instance.save()
        message = f"Setting 'blank' to {request.data} was successful."
        return Response({"detail": message})

    @detail_route(methods=["post"])
    def broadcast(self, request, pk):
        """
        REST API operation to (un-)broadcast the given projector.
        This method takes care, that all other projectors get the new requirements.

        It expects a POST request to
        /rest/core/projector/<pk>/broadcast/ without an argument
        """
        if config["projector_broadcast"] == 0:
            config["projector_broadcast"] = pk
            message = f"Setting projector {pk} as broadcast projector was successful."
        else:
            config["projector_broadcast"] = 0
            message = "Disabling broadcast was successful."
        return Response({"detail": message})

    @detail_route(methods=["post"])
    def set_projectiondefault(self, request, pk):
        """
        REST API operation to set a projectiondefault to the requested projector. The argument
        has to be an int representing the pk from the projectiondefault to be set.

        It expects a POST request to
        /rest/core/projector/<pk>/set_projectiondefault/ with the projectiondefault id as the argument
        """
        if not isinstance(request.data, int):
            raise ValidationError({"detail": "Data must be an int."})

        try:
            projectiondefault = ProjectionDefault.objects.get(pk=request.data)
        except ProjectionDefault.DoesNotExist:
            raise ValidationError(
                {
                    "detail": f"The projectiondefault with pk={request.data} was not found."
                }
            )
        else:
            projector_instance = self.get_object()
            projectiondefault.projector = projector_instance
            projectiondefault.save()

        return Response(
            f'Setting projectiondefault "{projectiondefault.display_name}" to projector {projector_instance.pk} was successful.'
        )


class TagViewSet(ModelViewSet):
    """
    API endpoint for tags.

    There are the following views: metadata, list, retrieve, create,
    partial_update, update and destroy.
    """

    access_permissions = TagAccessPermissions()
    queryset = Tag.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action == "metadata":
            # Every authenticated user can see the metadata.
            # Anonymous users can do so if they are enabled.
            result = self.request.user.is_authenticated or anonymous_is_enabled()
        elif self.action in ("create", "partial_update", "update", "destroy"):
            result = has_perm(self.request.user, "core.can_manage_tags")
        else:
            result = False
        return result


class ConfigViewSet(ModelViewSet):
    """
    API endpoint for the config.

    There are the following views: metadata, list, retrieve, update and
    partial_update.
    """

    access_permissions = ConfigAccessPermissions()
    queryset = ConfigStore.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action == "metadata":
            # Every authenticated user can see the metadata and list or
            # retrieve the config. Anonymous users can do so if they are
            # enabled.
            result = self.request.user.is_authenticated or anonymous_is_enabled()
        elif self.action in ("partial_update", "update"):
            # The user needs 'core.can_manage_logos_and_fonts' for all config values
            # starting with 'logo' and 'font'. For all other config values th euser needs
            # the default permissions 'core.can_manage_config'.
            pk = self.kwargs["pk"]
            if pk.startswith("logo") or pk.startswith("font"):
                result = has_perm(self.request.user, "core.can_manage_logos_and_fonts")
            else:
                result = has_perm(self.request.user, "core.can_manage_config")
        else:
            result = False
        return result

    def update(self, request, *args, **kwargs):
        """
        Updates a config variable. Only managers can do this.

        Example: {"value": 42}
        """
        key = kwargs["pk"]
        value = request.data.get("value")
        if value is None:
            raise ValidationError({"detail": "Invalid input. Config value is missing."})

        # Validate and change value.
        try:
            config[key] = value
        except ConfigNotFound:
            raise Http404
        except ConfigError as e:
            raise ValidationError({"detail": str(e)})

        # Return response.
        return Response({"key": key, "value": value})


class ChatMessageViewSet(ModelViewSet):
    """
    API endpoint for chat messages.

    There are the following views: metadata, list, retrieve and create.
    The views partial_update, update and destroy are disabled.
    """

    access_permissions = ChatMessageAccessPermissions()
    queryset = ChatMessage.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action in ("metadata", "create"):
            # We do not want anonymous users to use the chat even the anonymous
            # group has the permission core.can_use_chat.
            result = self.request.user.is_authenticated and has_perm(
                self.request.user, "core.can_use_chat"
            )
        elif self.action == "clear":
            result = has_perm(self.request.user, "core.can_use_chat") and has_perm(
                self.request.user, "core.can_manage_chat"
            )
        else:
            result = False
        return result

    def perform_create(self, serializer):
        """
        Customized method to inject the request.user into serializer's save
        method so that the request.user can be saved into the model field.
        """
        serializer.save(user=self.request.user)
        # Send chatter via autoupdate because users without permission
        # to see users may not have it but can get it now.
        inform_changed_data([self.request.user])

    @list_route(methods=["post"])
    def clear(self, request):
        """
        Deletes all chat messages.
        """
        # Collect all chat messages with their collection_string and id
        chatmessages = ChatMessage.objects.all()
        args = []
        for chatmessage in chatmessages:
            args.append((chatmessage.get_collection_string(), chatmessage.pk))
        chatmessages.delete()
        # Trigger autoupdate and setup response.
        if args:
            inform_deleted_data(args)
        return Response({"detail": "All chat messages deleted successfully."})


class ProjectorMessageViewSet(ModelViewSet):
    """
    API endpoint for messages.

    There are the following views: list, retrieve, create, update,
    partial_update and destroy.
    """

    access_permissions = ProjectorMessageAccessPermissions()
    queryset = ProjectorMessage.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action in ("create", "partial_update", "update", "destroy"):
            result = has_perm(self.request.user, "core.can_manage_projector")
        else:
            result = False
        return result


class CountdownViewSet(ModelViewSet):
    """
    API endpoint for Countdown.

    There are the following views: list, retrieve, create, update,
    partial_update and destroy.
    """

    access_permissions = CountdownAccessPermissions()
    queryset = Countdown.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        elif self.action in ("create", "partial_update", "update", "destroy"):
            result = has_perm(self.request.user, "core.can_manage_projector")
        else:
            result = False
        return result


class HistoryViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """
    API endpoint for History.

    There are the following views: list, retrieve, clear_history.
    """

    access_permissions = HistoryAccessPermissions()
    queryset = History.objects.all()

    def check_view_permissions(self):
        """
        Returns True if the user has required permissions.
        """
        if self.action in ("list", "retrieve", "clear_history"):
            result = self.get_access_permissions().check_permissions(self.request.user)
        else:
            result = False
        return result

    @list_route(methods=["post"])
    def clear_history(self, request):
        """
        Deletes and rebuilds the history.
        """
        # Collect all history objects with their collection_string and id.
        args = []
        for history_obj in History.objects.all():
            args.append((history_obj.get_collection_string(), history_obj.pk))

        # Delete history data and history (via CASCADE)
        HistoryData.objects.all().delete()

        # Trigger autoupdate.
        if args:
            inform_deleted_data(args)

        # Rebuild history.
        history_instances = History.objects.build_history()
        inform_changed_data(history_instances)

        # Setup response.
        return Response({"detail": "History was deleted successfully."})


# Special API views


class ServerTime(utils_views.APIView):
    """
    Returns the server time as UNIX timestamp.
    """

    http_method_names = ["get"]

    def get_context_data(self, **context):
        return now().timestamp()


class VersionView(utils_views.APIView):
    """
    Returns a dictionary with the OpenSlides version and the version of all
    plugins.
    """

    http_method_names = ["get"]

    def get_context_data(self, **context):
        Result = TypedDict(
            "Result",
            {
                "openslides_version": str,
                "openslides_license": str,
                "openslides_url": str,
                "plugins": List[Dict[str, str]],
            },
        )
        result: Result = dict(
            openslides_version=version,
            openslides_license=license,
            openslides_url=url,
            plugins=[],
        )
        # Versions of plugins.
        for plugin in settings.INSTALLED_PLUGINS:
            result["plugins"].append(
                {
                    "verbose_name": get_plugin_verbose_name(plugin),
                    "description": get_plugin_description(plugin),
                    "version": get_plugin_version(plugin),
                    "license": get_plugin_license(plugin),
                    "url": get_plugin_url(plugin),
                }
            )
        return result


class HistoryView(utils_views.APIView):
    """
    View to retrieve the history data of OpenSlides.

    Use query paramter timestamp (UNIX timestamp) to get all elements from begin
    until (including) this timestamp.
    """

    http_method_names = ["get"]

    def get_context_data(self, **context):
        """
        Checks if user is in admin group. If yes all history data until
        (including) timestamp are added to the response data.
        """
        if not in_some_groups(self.request.user.pk or 0, [GROUP_ADMIN_PK]):
            self.permission_denied(self.request)
        try:
            timestamp = int(self.request.query_params.get("timestamp", 0))
        except ValueError:
            raise ValidationError(
                {"detail": "Invalid input. Timestamp  should be an integer."}
            )
        data = []
        queryset = History.objects.select_related("full_data")
        if timestamp:
            queryset = queryset.filter(
                now__lte=datetime.datetime.fromtimestamp(timestamp)
            )
        for instance in queryset:
            data.append(
                {
                    "full_data": instance.full_data.full_data,
                    "element_id": instance.element_id,
                    "timestamp": instance.now.timestamp(),
                    "information": instance.information,
                    "user_id": instance.user.pk if instance.user else None,
                }
            )
        return data
