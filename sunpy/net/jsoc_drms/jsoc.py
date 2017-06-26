# -*- coding: utf-8 -*-
from __future__ import print_function, absolute_import

import os
import time
import warnings

import requests
import numpy as np
import astropy.units as u
import astropy.time
import astropy.table
from astropy.utils.misc import isiterable
import drms

from sunpy import config
from sunpy.time import parse_time, TimeRange
from sunpy.net.download import Downloader, Results
from sunpy.net.attr import and_
from sunpy.net.jsoc.attrs import walker
from sunpy.extern.six.moves import urllib
from sunpy.extern import six

__all__ = ['JSOCClient', 'JSOCResponse']

JSOC_INFO_URL = 'http://jsoc.stanford.edu/cgi-bin/ajax/jsoc_info'
JSOC_EXPORT_URL = 'http://jsoc.stanford.edu/cgi-bin/ajax/jsoc_fetch'
BASE_DL_URL = 'http://jsoc.stanford.edu'


class JSOCResponse(object):
    def __init__(self, table=None):
        """
        table : `astropy.table.Table`
        """

        self.table = table
        self.query_args = None
        self.requestIDs = None

    def __str__(self):
        return str(self.table)

    def __repr__(self):
        return repr(self.table)

    def _repr_html_(self):
        return self.table._repr_html_()

    def __len__(self):
        if self.table is None:
            return 0
        else:
            return len(self.table)

    def append(self, table):
        if self.table is None:
            self.table = table
        else:
            self.table = astropy.table.vstack([self.table, table])


class JSOCClient(object):
    
    def query(self, *query, **kwargs):

        return_results = JSOCResponse()
        query = and_(*query)
        blocks = []
        for block in walker.create(query):
            iargs = kwargs.copy()
            iargs.update(block)
            blocks.append(iargs)

            return_results.append(self._lookup_records(iargs))

        return_results.query_args = blocks

        return return_results

    def request_data(self, jsoc_response, **kwargs):
        """
        Request that JSOC stages the data for download.

        Parameters
        ----------
        jsoc_response : JSOCResponse object
            The results of a query

        Returns
        -------
        requestIDs : list of strings
            List of the JSOC request identifiers

        """
        # A little (hidden) debug feature
        return_responses = kwargs.pop('return_resp', False)
        if len(kwargs):
            warn_message = "request_data got unexpected keyword arguments {0}"
            raise TypeError(warn_message.format(list(kwargs.keys())))

        # Do a multi-request for each query block
        responses = []
        requestIDs = []
        for block in jsoc_response.query_args:
            # Do a multi-request for each query block
            responses.append(self._multi_request(**block))
            for i, response in enumerate(responses[-1]):
                if response.status_code != 200:
                    warn_message = "Query {0} retuned code {1}"
                    warnings.warn(
                        Warning(warn_message.format(i, response.status_code)))
                    responses.pop(i)
                elif response.json()['status'] != 2:
                    warn_message = "Query {0} retuned status {1} with error {2}"
                    json_response = response.json()
                    json_status = json_response['status']
                    json_error = json_response['error']
                    warnings.warn(Warning(warn_message.format(i, json_status,
                                                              json_error)))
                    responses[-1].pop(i)

            # Extract the IDs from the JSON
            requestIDs += [response.json()['requestid'] for response in responses[-1]]

        if return_responses:
            return responses

        return requestIDs

    def check_request(self, requestIDs):
        """
        Check the status of a request and print out a message about it

        Parameters
        ----------
        requestIDs : list or string
            A list of requestIDs to check

        Returns
        -------
        status : list
            A list of status' that were returned by JSOC
        """
        # Convert IDs to a list if not already
        if not isiterable(requestIDs) or isinstance(requestIDs, six.string_types):
            requestIDs = [requestIDs]

        allstatus = []
        for request_id in requestIDs:
            u = self._request_status(request_id)
            status = int(u.json()['status'])

            if status == 0:  # Data ready to download
                print("Request {0} was exported at {1} and is ready to "
                      "download.".format(u.json()['requestid'],
                                         u.json()['exptime']))
            elif status == 1:
                print_message = "Request {0} was submitted {1} seconds ago, " \
                                "it is not ready to download."
                print(print_message.format(u.json()['requestid'],
                                           u.json()['wait']))
            else:
                print_message = "Request returned status: {0} with error: {1}"
                json_status = u.json()['status']
                json_error = u.json()['error']
                print(print_message.format(json_status, json_error))

            allstatus.append(status)

        return allstatus

    def get(self, jsoc_response, path=None, overwrite=False, progress=True,
            max_conn=5, downloader=None, sleep=10):
        """
        Make the request for the data in jsoc_response and wait for it to be
        staged and then download the data.

        Parameters
        ----------
        jsoc_response : JSOCResponse object
            A response object

        path : string
            Path to save data to, defaults to SunPy download dir

        overwrite : bool
            Replace files with the same name if True

        progress : bool
            Print progress info to terminal

        max_conns : int
            Maximum number of download connections.

        downloader: `sunpy.download.Downloader` instance
            A Custom downloader to use

        sleep : int
            The number of seconds to wait between calls to JSOC to check the status
            of the request.

        Returns
        -------
        results : a :class:`sunpy.net.vso.Results` instance
            A Results object
        """

        # Make staging request to JSOC
        requestIDs = self.request_data(jsoc_response)
        # Add them to the response for good measure
        jsoc_response.requestIDs = requestIDs
        time.sleep(sleep/2.)

        r = Results(lambda x: None, done=lambda maps: [v['path'] for v in maps.values()])

        while requestIDs:
            for i, request_id in enumerate(requestIDs):
                u = self._request_status(request_id)

                if progress:
                    self.check_request(request_id)

                if u.status_code == 200 and u.json()['status'] == '0':
                    rID = requestIDs.pop(i)
                    r = self.get_request(rID, path=path, overwrite=overwrite,
                                         progress=progress, results=r)

                else:
                    time.sleep(sleep)

        return r

    def get_request(self, requestIDs, path=None, overwrite=False, progress=True,
                    max_conn=5, downloader=None, results=None):
        """
        Query JSOC to see if request_id is ready for download.

        If the request is ready for download, download it.

        Parameters
        ----------
        requestIDs : list or string
            One or many requestID strings

        path : string
            Path to save data to, defaults to SunPy download dir

        overwrite : bool
            Replace files with the same name if True

        progress : bool
            Print progress info to terminal

        max_conns : int
            Maximum number of download connections.

        downloader : `sunpy.download.Downloader` instance
            A Custom downloader to use

        results: Results instance
            A Results manager to use.

        Returns
        -------
        res: Results
            A Results instance or None if no URLs to download
        """

        # Convert IDs to a list if not already

        if not isiterable(requestIDs) or isinstance(requestIDs, six.string_types):
            requestIDs = [requestIDs]

        if path is None:
            path = config.get('downloads', 'download_dir')
        path = os.path.expanduser(path)

        if downloader is None:
            downloader = Downloader(max_conn=max_conn, max_total=max_conn)

        # A Results object tracks the number of downloads requested and the
        # number that have been completed.
        if results is None:
            results = Results(lambda _: downloader.stop())

        urls = []
        for request_id in requestIDs:
            u = self._request_status(request_id)

            if u.status_code == 200 and u.json()['status'] == '0':
                for ar in u.json()['data']:
                    is_file = os.path.isfile(os.path.join(path, ar['filename']))
                    if overwrite or not is_file:
                        url_dir = BASE_DL_URL + u.json()['dir'] + '/'
                        urls.append(urllib.parse.urljoin(url_dir, ar['filename']))

                    else:
                        print_message = "Skipping download of file {} as it " \
                                        "has already been downloaded"
                        print(print_message.format(ar['filename']))
                        # Add the file on disk to the output
                        results.map_.update({ar['filename']:
                                            {'path': os.path.join(path, ar['filename'])}})

                if progress:
                    print_message = "{0} URLs found for download. Totalling {1}MB"
                    print(print_message.format(len(urls), u.json()['size']))

            else:
                if progress:
                    self.check_request(request_id)

        if urls:
            for url in urls:
                downloader.download(url, callback=results.require([url]),
                                    errback=lambda x: print(x), path=path)

        else:
            # Make Results think it has finished.
            results.require([])
            results.poke()

        return results

    def _process_time(self, time):
        """
        Take a UTC time string or datetime instance and generate a astropy.time
        object in TAI frame. Alternatively convert a astropy time object to TAI

        Parameters
        ----------
        time: six.string_types or datetime or astropy.time
            Input time

        Returns
        -------
        datetime, in TAI
        """
        # Convert from any input (in UTC) to TAI
        if isinstance(time, six.string_types):
            time = parse_time(time)

        time = astropy.time.Time(time, scale='utc')
        time = time.tai  # Change the scale to TAI

        return time.datetime

    def _make_recordset(self, start_time, end_time, series, wavelength='',
                        segment='', **kwargs):
        # Build the dataset string
        # Extract and format Wavelength
        if wavelength:
            if not series.startswith('aia'):
                raise TypeError("This series does not support the wavelength attribute.")
            else:
                if isinstance(wavelength, list):
                    wavelength = [int(np.ceil(wave.to(u.AA).value)) for wave in wavelength]
                    wavelength = str(wavelength)
                else:
                    wavelength = '[{0}]'.format(int(np.ceil(wavelength.to(u.AA).value)))

        # Extract and format segment
        if segment != '':
            segment = '{{{segment}}}'.format(segment=segment)

        sample = kwargs.get('sample', '')
        if sample:
            sample = '@{}s'.format(sample)

        dataset = '{series}[{start}-{end}{sample}]{wavelength}{segment}'.format(
                   series=series, start=start_time.strftime("%Y.%m.%d_%H:%M:%S_TAI"),
                   end=end_time.strftime("%Y.%m.%d_%H:%M:%S_TAI"),
                   sample=sample,
                   wavelength=wavelength, segment=segment)

        return dataset

    def _make_query_payload(self, start_time, end_time, series, notify=None,
                            protocol='FITS', compression='rice', **kwargs):
        """
        Build the POST payload for the query parameters
        """

        if protocol.upper() == 'FITS' and compression and compression.lower() == 'rice':
            jprotocol = 'FITS,compress Rice'
        elif protocol.upper() == 'FITS':
            jprotocol = 'FITS, **NONE**'
        else:
            jprotocol = protocol

        if not notify:
            raise ValueError("JSOC queries now require a valid email address "
                             "before they will be accepted by the server")

        dataset = self._make_recordset(start_time, end_time, series, **kwargs)
        kwargs.pop('wavelength', None)
        kwargs.pop('sample', None)

        # Build full POST payload
        payload = {'ds': dataset,
                   'format': 'json',
                   'method': 'url',
                   'notify': notify,
                   'op': 'exp_request',
                   'process': 'n=0|no_op',
                   'protocol': jprotocol,
                   'requestor': 'none',
                   'filenamefmt': '{0}.{{T_REC:A}}.{{CAMERA}}.{{segment}}'.format(series)}

        payload.update(kwargs)
        return payload

    def _send_jsoc_request(self, start_time, end_time, series, notify=None,
                           protocol='FITS', compression='rice', **kwargs):
        """
        Request that JSOC stages data for download

        This routine puts in a POST request to JSOC
        """

        payload = self._make_query_payload(start_time, end_time, series,
                                           notify=notify, protocol=protocol,
                                           compression=compression, **kwargs)

        r = requests.post(JSOC_EXPORT_URL, data=payload)

        if r.status_code != 200:
            exception_message = "JSOC POST Request returned code {0}"
            raise Exception(exception_message.format(r.status_code))

        return r, r.json()

    def _lookup_records(self, iargs):
        """
        Do a LookData request to JSOC to workout what results the query returns
        """
        keywords = ['DATE', 'TELESCOP', 'INSTRUME', 'T_OBS', 'WAVELNTH',
                    'WAVEUNIT']

        if not all([k in iargs for k in ('start_time', 'end_time', 'series')]):
            error_message = "Both Time and Series must be specified for a "\
                            "JSOC Query"
            raise ValueError(error_message)

        iargs['start_time'] = self._process_time(iargs['start_time'])
        iargs['end_time'] = self._process_time(iargs['end_time'])

        postthis = {'ds': self._make_recordset(**iargs),
                    'op': 'rs_list',
                    'key': str(keywords)[1:-1].replace(' ', '').replace("'", ''),
                    'seg': '**NONE**',
                    'link': '**NONE**'}
        c = drms.Client()
        r = c.query(postthis['ds'], key=postthis['key'])
        if r is None:
            return astropy.table.Table()
        else:
            return astropy.table.Table.from_pandas(r)

    def _multi_request(self, **kwargs):
        """
        Make a series of requests to avoid the 100GB limit
        """
        start_time = kwargs.pop('start_time', None)
        end_time = kwargs.pop('end_time', None)
        series = kwargs.pop('series', None)
        if any(x is None for x in (start_time, end_time, series)):
            return []
        start_time = self._process_time(start_time)
        end_time = self._process_time(end_time)
        tr = TimeRange(start_time, end_time)
        returns = []
        response, json_response = self._send_jsoc_request(start_time, end_time,
                                                          series, **kwargs)

        # We skip these lines because a massive request is not a practical test.
        error_response = 'Request exceeds max byte limit of 100000MB'
        if (json_response['status'] == 3 and
                json_response['error'] == error_response):  # pragma: no cover
            returns.append(self._multi_request(tr.start(), tr.center(), series, **kwargs)[0])
            # pragma: no cover
            returns.append(self._multi_request(tr.center(), tr.end(), series, **kwargs)[0])
            # pragma: no cover

        else:
            returns.append(response)

        return returns

    def _request_status(self, request_id):
        """
        GET the status of a request ID
        """
        payload = {'op': 'exp_status', 'requestid': request_id}
        u = requests.get(JSOC_EXPORT_URL, params=payload)

        return u

    @classmethod
    def _can_handle_query(cls, *query):
        chkattr = ['Series', 'Protocol', 'Notify', 'Compression', 'Wavelength',
                   'Time', 'Segment']

        return all([x.__class__.__name__ in chkattr for x in query])
