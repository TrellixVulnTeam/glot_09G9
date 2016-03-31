import datetime
import tabulate
import colorama as C
import asyncio
import os
import tarfile
from git import Repo
import shutil
import lxml.etree
import tempfile
import stat
import uuid

import glot.transfer

_repo_locations = {
    'goosefoot': 'https://github.com/go-smart/glossia-container-goosefoot-control'
}


class GlotActor:
    def __init__(self, verbose, force, destination, color):
        self._verbose = verbose
        self._force = force
        self._destination = destination
        self._color = color

    def set_log(self, log):
        self._log = log

    def set_make_call(self, mc):
        self._mc = mc

    @asyncio.coroutine
    def cancel(self, guid):
        log = self._log
        mc = self._mc

        success = yield from mc('cancel', guid.upper())

        if success:
            log.info('Cancelled [%s]' % guid)
        else:
            log.error('Could not cancel [%s]' % guid)

    @asyncio.coroutine
    def launch(self, gssa_xml, tmp_subdirectory, tmp_directory, skip_clean, input_files, definition_files):
        gssa = lxml.etree.parse(gssa_xml)
        log = self._log
        mc = self._mc

        # We tar the definition files into one object for transferring and add
        # it to the definition node
        if definition_files:
            definition_tmp = tempfile.NamedTemporaryFile(suffix='.tar.gz', dir=tmp_directory, delete=False)
            definition_tar = tarfile.open(fileobj=definition_tmp, mode='w:gz')
            for definition_file in definition_files:
                definition_tar.add(definition_file, os.path.basename(definition_file))
                log.debug("Added [%s]" % os.path.basename(definition_file))
            definition_tar.close()
            definition_tmp.close()

            # Note that this makes the file global readable - we assume the
            # parent of the tmp directory is used to control permissions
            os.chmod(definition_tmp.name, stat.S_IROTH | stat.S_IRGRP | stat.S_IRUSR)

            log.debug("Made temporary tar at %s" % definition_tmp.name)
            definition_node = gssa.find('.//definition')
            location_remote = os.path.join('/tmp', 'gssa-transferrer', os.path.basename(definition_tmp.name))
            definition_node.set('location', location_remote)

        # Do the same with the input surfaces
        if input_files:
            input_tmp = tempfile.NamedTemporaryFile(suffix='.tar.gz', dir=tmp_directory, delete=False)
            input_tar = tarfile.open(fileobj=input_tmp, mode='w:gz')
            for input_file in input_files:
                input_tar.add(input_file, os.path.basename(input_file))
                log.debug("Added [%s]" % os.path.basename(input_file))
            input_tar.close()
            input_tmp.close()

            # Note that this makes the file global readable - we assume the
            # parent of the tmp directory is used to control permissions
            os.chmod(input_tmp.name, stat.S_IROTH | stat.S_IRGRP | stat.S_IRUSR)

            log.debug("Made temporary tar at %s" % input_tmp.name)
            input_node = lxml.etree.SubElement(gssa.find('.//transferrer'), 'input')
            location_remote = os.path.join('/tmp', 'gssa-transferrer', os.path.basename(input_tmp.name))
            input_node.set('location', location_remote)

        # Generate a simulation ID
        guid = uuid.uuid1()

        # Run the simulation
        guid = str(guid)
        gssa_string = lxml.etree.tostring(gssa, encoding="unicode")
        yield from mc('init', guid)
        log.info("Initiated...")
        yield from mc('update_settings_xml', guid, gssa_string)
        log.info("Sent XML...")
        yield from mc('finalize', guid, tmp_subdirectory)
        log.info("Finalized settings...")
        yield from mc('start', guid)
        log.info("Started.")

        # These may already have been removed
        for tmp in definition_tmp, input_tmp:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    @asyncio.coroutine
    def status(self, guid):
        log = self._log
        mc = self._mc

        simulation = yield from mc('retrieve_status', guid.upper())

        if not simulation:
            log.error('Simulation [%s] not found' % guid)
        else:
            table = simulation.items()
            print(tabulate.tabulate(table))

    @asyncio.coroutine
    def results(self, guid, target, include_diagnostic, inspect_diagnostic):
        log = self._log
        mc = self._mc

        filename = None
        include_diagnostic = include_diagnostic or inspect_diagnostic

        if target is None:
            log.warn(
                "No target given, assuming we should provide "
                "a target for a local Glossia"
            )
            srv = yield from glot.transfer.OneFileHttpServer.make(log, '%s-results.tgz' % guid)
        else:
            srv = None

        success = yield from mc('request_results', guid.upper(), target)

        if not success:
            log.error('Simulation not found')

        if srv is not None:
            if success:
                filename = yield from srv.wait()
            yield from srv.close()

        if not target and not self._destination:
            self._destination = '.'

        if include_diagnostic:
            yield from self._diagnostic(guid, target, inspect_diagnostic)

        if not target and inspect_diagnostic:
            to = self._destination
            os.makedirs(to, exist_ok=True)
            with tarfile.open(filename) as f:
                f.extractall(path=to)

    @asyncio.coroutine
    def search(self, limit, server_limit, sort, guid, fancy=False):
        log = self._log
        mc = self._mc
        color = self._color

        definitions = yield from mc('search', guid.upper() if guid else '', server_limit)

        headers = [
            'GUID',
            'Set Up',
            'Last status',
            '%',
            '',
            'Completed'
        ]

        for d in definitions.values():
            if d['status']:
                try:
                    float(d['status']['percentage'])
                except:
                    d['status']['percentage'] = False

        g, d = "", ""
        table = []
        for g, d in definitions.items():
            if not d:
                raise Exception("Empty definition!")
            try:
                status = d['status'] if d['status'] else {'percentage': False, 'message': None, 'timestamp': None}
                d['status'] = status

                table.append([
                    g,
                    'Y' if d['finalized'] else 'N',
                    '' if not status['timestamp'] else datetime.datetime.fromtimestamp(status['timestamp']).strftime('%A %d, %B %Y :: %H:%M:%S'),
                    '' if not status['percentage'] else ("%.2lf" % status['percentage']),
                    '' if not status['message'] else status['message'].replace('\n', ' ')[0:60],
                    '-' if not d['exit_status'] else ('Y' if d['exit_status'][0] in (True, 'SUCCESS') else 'N')
                ])
            except Exception as e:
                log.error('Could not format status for a simulation')
                log.error(str(e))
                log.error(g)
                # Work around txaio's {} parsing
                log.error(str(d).replace('{', '[').replace('}', ']'))

        if sort == 'timestamp':
            table.sort(key=lambda r: -definitions[r[0]]['status']['timestamp'] if definitions[r[0]]['status']['timestamp'] else 0)
        elif sort == 'guid':
            table.sort(key=lambda r: r[0])

        if limit:
            table = table[:limit]

        if color:
            ce = {'-': C.Fore.YELLOW, 'Y': C.Fore.GREEN, 'N': C.Fore.RED}
            table = [[ce[d[-1]] + di + C.Style.RESET_ALL for di in d] for d in table]

        print(tabulate.tabulate(table, headers=headers, tablefmt=('fancy_grid' if fancy else 'simple')))

    @asyncio.coroutine
    def diagnostic(self, guid, target, inspect):
        log = self._log
        mc = self._mc

        filename = None

        if target is None:
            log.warn(
                "No target given, assuming we should provide "
                "a target for a local Glossia"
            )
            srv = yield from glot.transfer.OneFileHttpServer.make(log, '%s-diagnostic.tgz' % guid)
        else:
            srv = None

        files = yield from mc('request_diagnostic', guid.upper(), target)

        if srv is not None:
            if files:
                filename = yield from srv.wait()
            else:
                srv.cancel()
            srv.close()

        if files:
            log.info("FILES:\n\t%s" % "\n\t".join(["[%s]: [%s]" % t for t in files.items()]))
        else:
            log.warn("No simulation diagnostics found")

        to = self._destination if not target else None
        if inspect:
            log.debug("Inspect")
            if filename is None:
                if len(files) < 1:
                    raise RuntimeError("No diagnostic files returned")
                elif len(files) > 1:
                    raise RuntimeError("Multiple diagnostic files, run inspect manually")

                filename = files.popitem()
                log.debug("Using %s: %s" % filename)
                filename = filename[0]

            with tarfile.open(filename, 'r') as t:
                members = t.getmembers()
                names = [m.name for m in members if not m.isdir()]
                prefix = os.path.commonprefix(names)

            log.debug("Inspecting %s" % filename)
            self._inspect(filename, os.path.join(to, prefix) if to else None)

        return filename

    def inspect(self, archive, to, mode='goosefoot'):
        log = self._log
        verbose = self._verbose
        force = self._force

        if not os.path.exists(archive):
            raise RuntimeError("You must supply a diagnostic archive")

        if to:
            path = to
        else:
            path = os.path.splitext(archive)[0]

        if not force:
            if os.path.exists(path):
                raise FileExistsError("Run with --force to remove existing diagnostic directory (%s)" % path)
        else:
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                pass

        rootpath = path
        log.debug("Extracting to {path}".format(path=path))

        repo_location = _repo_locations[mode]
        log.debug("Cloning control from {loc}".format(loc=repo_location))
        Repo.clone_from(repo_location, path)

        log.debug("Opening diagnostic archive {arc}".format(arc=archive))
        with tarfile.open(archive, 'r') as t:
            members = t.getmembers()
            names = [m.name for m in members if not m.isdir()]
            prefix = os.path.commonprefix(names)
            log.debug("Stripping prefix {prefix}".format(prefix=prefix))

            inp = os.path.join(prefix, 'input')
            inpfinal = os.path.join(prefix, 'input.final')
            try:
                t.getmember(inp)
            except KeyError:
                try:
                    t.getmember(inpfinal)
                except KeyError:
                    path = os.path.join(path, 'input')
                    os.makedirs(path)

            for m in members:
                if m.name.startswith(prefix):
                    outpath = os.path.join(path, m.name[len(prefix):])
                    outpath = outpath.replace('input.final', 'input')

                    if verbose:
                        log.info("{fm} --> {to}".format(fm=m.name, to=outpath))

                    if m.isdir():
                        os.makedirs(outpath)
                    else:
                        with open(outpath, 'wb') as f, t.extractfile(m) as g:
                            shutil.copyfileobj(g, f)

        log.info("Done extracting")

        if mode is 'goosefoot':
            shutil.copyfile(
                os.path.join(rootpath, 'input', 'settings.xml'),
                os.path.join(rootpath, 'settings', 'settings.xml')
            )