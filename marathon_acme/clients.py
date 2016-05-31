import json
import treq

from twisted.internet import reactor
from twisted.python import log
from twisted.web import client
from twisted.web.http import OK

from uritools import uricompose, urisplit

# Twisted's default HTTP11 client factory is way too verbose
client._HTTP11ClientFactory.noisy = False


def json_content(response):
    # Workaround for treq not treating JSON as UTF-8 by default (RFC7158)
    # https://github.com/twisted/treq/pull/126
    # See this discussion: http://stackoverflow.com/q/9254891
    d = response.text(encoding='utf-8')
    return d.addCallback(json.loads)


class JsonClient(object):
    debug = False
    timeout = 5

    def __init__(self, url=None, agent=None, clock=reactor):
        """
        Create a client with the specified default URL.
        """
        self.url = url
        self._agent = agent
        self._pool = client.HTTPConnectionPool(clock, persistent=False)

    def _log_request_response(self, response, method, path, data):
        log.msg('%s %s with %s returned: %s' % (
            method, path, data, response.code))
        return response

    def _log_request_error(self, failure, url):
        log.err(failure, 'Error performing request to %s' % (url,))
        return failure

    def _compose_url(self, url, kwargs):
        """
        Compose a URL starting with the given URL (or self.url if that URL is
        None) and using the values in kwargs.

        :param str url:
            The base URL to use. If None, ``self.url`` will be used instead.
        :param dict kwargs:
            A dictionary of values to override in the base URL. Relevant keys
            will be popped from the dictionary.
        """
        if url is None:
            url = self.url

        compose_kwargs = {}
        if url is not None:
            compose_kwargs.update(urisplit(url)._asdict())

        compose_keys = ['scheme', 'authority', 'path', 'query', 'fragment',
                        'userinfo', 'host', 'port']
        for key in compose_keys:
            if key in kwargs:
                compose_kwargs[key] = kwargs.pop(key)

        return uricompose(**compose_kwargs)

    def request(self, method, url=None, json_data=None, raise_for_status=False,
                **kwargs):
        """
        Perform a request. A number of basic defaults are set on the request
        that make using a JSON API easier. These defaults can be overridden by
        setting the parameters in the keyword args.

        :param: method:
            The HTTP method to use (example is `GET`).
        :param: url:
            The URL to use. The default value is the URL this client was
            created with (`self.url`) (example is `http://localhost:8080`)
        :param: json_data:
            A python data structure that will be converted to a JSON string
            using `json.dumps` and used as the request body.
        :param: raise_for_status:
            Whether to raise an error for a 4xx or 5xx response code.
        :param: kwargs:
            Any other parameters that will be passed to `treq.request`, for
            example headers. Or any URL parameters to override, for example
            path, query or fragment.
        """
        url = self._compose_url(url, kwargs)

        data = None
        headers = {'Accept': 'application/json'}

        # Add JSON body if there is JSON data
        if json_data is not None:
            data = json.dumps(json_data).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        request_kwargs = {
            'headers': headers,
            'data': data,
            'pool': self._pool,
            'agent': self._agent,
            'timeout': self.timeout
        }
        request_kwargs.update(kwargs)

        d = treq.request(method, url, **request_kwargs)

        if self.debug:
            d.addCallback(self._log_request_response, method, url, data)

        d.addErrback(self._log_request_error, url)

        if raise_for_status:
            d.addCallback(self._raise_for_status, url)

        return d

    def get_json(self, **kwargs):
        """
        Perform a GET request to the given path and return the JSON response.
        """
        d = self.request('GET', **kwargs)
        return d.addCallback(json_content)

    def _raise_for_status(self, response, url):
        """
        Raises an `HTTPError` if the response did not succeed.
        Adapted from the Requests library:
        https://github.com/kennethreitz/requests/blob/v2.8.1/requests/models.py#L825-L837
        """
        http_error_msg = ''

        if 400 <= response.code < 500:
            http_error_msg = '%s Client Error for url: %s' % (response.code,
                                                              url)

        elif 500 <= response.code < 600:
            http_error_msg = '%s Server Error for url: %s' % (response.code,
                                                              url)

        if http_error_msg:
            raise HTTPError(http_error_msg, response)

        return response


class HTTPError(IOError):
    """
    Error raised for 4xx and 5xx response codes.
    """
    def __init__(self, message, response):
        self.response = response
        super(HTTPError, self).__init__(message)


class MarathonClient(JsonClient):

    def get_json_field(self, field, **kwargs):
        """
        Perform a GET request and get the contents of the JSON response.

        Marathon's JSON responses tend to contain an object with a single key
        which points to the actual data of the response. For example /v2/apps
        returns something like {"apps": [ {"app1"}, {"app2"} ]}. We're
        interested in the contents of "apps".

        This method will raise an error if:
        * There is an error response code
        * The field with the given name cannot be found
        """
        return self.get_json(raise_for_status=True, **kwargs).addCallback(
            self._get_json_field, field)

    def _get_json_field(self, response_json, field_name):
        """
        Get a JSON field from the response JSON.

        :param: response_json:
            The parsed JSON content of the response.
        :param: field_name:
            The name of the field in the JSON to get.
        """
        if field_name not in response_json:
            raise KeyError('Unable to get value for "%s" from Marathon '
                           'response: "%s"' % (
                               field_name, json.dumps(response_json),))

        return response_json[field_name]

    def get_event_subscriptions(self):
        """
        Get the current Marathon event subscriptions, returning a list of
        callback URLs.
        """
        return self.get_json_field(
            'callbackUrls', path='/v2/eventSubscriptions')

    def post_event_subscription(self, callback_url):
        """
        Post a new Marathon event subscription with the given callback URL.
        """
        d = self.request('POST',
                         path='/v2/eventSubscriptions',
                         query={'callbackUrl': callback_url})
        return d.addCallback(lambda response: response.code == OK)

    def delete_event_subscription(self, callback_url):
        """
        Delete the Marathon event subscription with the given callback URL.
        """
        d = self.request('DELETE',
                         path='/v2/eventSubscriptions',
                         query={'callbackUrl': callback_url})
        return d.addCallback(lambda response: response.code == OK)

    def get_apps(self):
        """
        Get the currently running Marathon apps, returning a list of app
        definitions.
        """
        return self.get_json_field('apps', path='/v2/apps')

    def get_app(self, app_id):
        """
        Get information about the app with the given app ID.
        """
        return self.get_json_field('app', path='/v2/apps%s' % (app_id,))

    def get_app_tasks(self, app_id):
        """
        Get the currently running tasks for the app with the given app ID,
        returning a list of task definitions.
        """
        return self.get_json_field(
            'tasks', path='/v2/apps%s/tasks' % (app_id,))
