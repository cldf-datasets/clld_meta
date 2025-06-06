import csv
import sys
import zipfile
from collections import Counter, namedtuple
from itertools import chain, islice
from multiprocessing import Pool
from pathlib import Path

from cldfbench import Dataset as BaseDataset
from cldfbench.cldf import CLDFSpec

from cldf_meta import download as dl, zipdata
from cldf_meta.util import loggable_progress, path_contains

CLDFError = namedtuple('CLDFError', 'record_no file reason')
DataArchive = namedtuple('DataArchive', 'record_no file_id path')
Download = namedtuple('Download', 'url destination checksum')


def download_path(data_dir, record_no, file_path):
    output_folder = (data_dir / record_no).resolve()
    output_file = (output_folder / file_path).resolve()
    # make sure we don't leave the designated download area
    assert data_dir.resolve() in output_file.parents
    return output_file


def download_datasets(downloads, access_token=None):
    urls = (download.url for download in downloads)
    if access_token:
        urls = (dl.add_access_token(url, access_token) for url in urls)
    dls = dl.download_all(loggable_progress(urls, file=sys.stderr))
    for raw_data, download in zip(dls, downloads):
        dl.validate_checksum(download.checksum, raw_data)
        download.destination.parent.mkdir(parents=True, exist_ok=True)
        download.destination.write_bytes(raw_data)


def is_blacklisted(blacklist, record):
    return (
        record.get('doi') in blacklist
        or record.get('conceptdoi') in blacklist)


def might_be_zip(file):
    return file['file_path'].endswith('.zip')


# FIXME not happy with that function name
def collect_dataset_stats(record_no, zipreader):
    values = [
        (r['languageReference'], r.get('parameterReference'))
        for r in zipreader.iterrows(
            'ValueTable', 'languageReference', 'parameterReference')
        if r.get('languageReference')]
    lang_values = Counter(lg for lg, _ in values)
    # XXX: count parameters and concepts separately?
    #  if so -- how?
    # # FIXME: tbqh I don't remember what this is for?
    # lang_features = Counter((lg, p) for lg, p in values if p)

    forms = list(zipreader.iterrows('FormTable', 'languageReference'))
    lang_forms = Counter(
        r['languageReference']
        for r in forms
        if r.get('languageReference'))

    entries = list(zipreader.iterrows('EntryTable', 'languageReference'))
    lang_entries = Counter(
        r['languageReference']
        for r in entries
        if r.get('languageReference'))

    examples = list(zipreader.iterrows('ExampleTable', 'languageReference'))
    lang_examples = Counter(
        lid
        for ex in examples
        if (lid := ex.get('languageReference')))

    lang_iter = chain(lang_values, lang_forms, lang_examples, lang_entries)
    langtable = {
        r['id']: r.get('glottocode') or r.get('iso639P3code') or r.get('id')
        for r in zipreader.iterrows(
            'LanguageTable', 'id', 'glottocode', 'iso639P3code')
        if r.get('id')}
    langs = {v: (langtable.get(v) or v) for v in lang_iter}

    # TODO count concepticon ids?
    parameter_count = sum(1 for _ in zipreader.iterrows('ParameterTable', 'id'))

    return {
        'record_no': record_no,
        'module': zipreader.cldf_module(),
        'value_count': len(values),
        'form_count': len(forms),
        'entry_count': len(entries),
        'parameter_count': parameter_count,
        'example_count': len(examples),
        'langs': langs,
        'lang_values': lang_values,
        # 'lang_features': lang_features,
        'lang_forms': lang_forms,
        'lang_entries': lang_entries,
        'lang_examples': lang_examples,
    }


def _stats_from_zip(data_archive):
    record_no, file_id, zip_path = data_archive
    found_data = False
    with zipfile.ZipFile(zip_path) as zip:
        file_tree = {Path(info.filename): info for info in zip.infolist()}
        for path, info in file_tree.items():
            if path.suffix != '.json':
                continue
            # Filter out test suites and raw upstream data in cldfbenches.
            if path_contains(path, 'raw|tests?'):
                continue
            with zip.open(info) as f:
                cldf_md = zipdata.get_cldf_json(f)
            if cldf_md is None:
                continue
            zipreader = zipdata.ZipDataReader(
                zip, file_tree, path.parent, cldf_md)
            found_data = True
            yield collect_dataset_stats(record_no, zipreader), None
    if not found_data:
        yield None, CLDFError(record_no, file_id, 'nocldf')


def stats_from_zip(data_archive):
    return list(_stats_from_zip(data_archive))


def raw_stats_to_glottocode_stats(stats, by_glottocode, by_isocode):
    original_language_count = len(stats['langs'])

    lid_to_glottocode = {
        lid: (by_glottocode.get(guess) or by_isocode[guess]).id
        for lid, guess in stats['langs'].items()
        if guess in by_glottocode or guess in by_isocode}

    glottocode_to_lids = {}
    for lid, glottocode in lid_to_glottocode.items():
        if glottocode not in glottocode_to_lids:
            glottocode_to_lids[glottocode] = []
        glottocode_to_lids[glottocode].append(lid)

    def accumulate_counts(count_map):
        return {
            glottocode: sum_of_counts
            for glottocode, lids in glottocode_to_lids.items()
            if (sum_of_counts := sum(count_map.get(lid, 0) for lid in lids)) != 0}

    return {
        'record_no': stats['record_no'],
        'module': stats['module'],
        'lang_count': original_language_count,
        'glottocode_count': len(glottocode_to_lids),
        'value_count': stats['value_count'],
        'form_count': stats['form_count'],
        'entry_count': stats['entry_count'],
        'parameter_count': stats['parameter_count'],
        'example_count': stats['example_count'],
        'langs': list(glottocode_to_lids),
        'glottocode_values': accumulate_counts(stats['lang_values']),
        # 'glottocode_features': accumulate_counts(stats['lang_features']),
        'glottocode_forms': accumulate_counts(stats['lang_forms']),
        'glottocode_entries': accumulate_counts(stats['lang_entries']),
        'glottocode_examples': accumulate_counts(stats['lang_examples']),
    }


class ErrorFilter:
    def __init__(self):
        self.errors = []

    def filter(self, iterable):
        for val, err in iterable:
            if err is not None:
                self.errors.append(err)
            if val is not None:
                yield val


def languages_from_dataset_stats(dataset_stats, languoids_by_id):
    all_glottocodes = sorted({
        lid
        for stats in dataset_stats
        for lid in stats['langs']})

    def macroarea(lg):
        m = lg.macroareas
        return m[0].name if m else ''

    return [
        {
            'ID': lid,
            'Name': languoids_by_id[lid].name,
            'Macroarea': macroarea(languoids_by_id[lid]),
            'Latitude': languoids_by_id[lid].latitude,
            'Longitude': languoids_by_id[lid].longitude,
            'Glottocode': lid,
            'ISO639P3code': (languoids_by_id[lid].iso or ''),
        }
        for lid in all_glottocodes]


def datasets_from_dataset_stats(dataset_stats):
    datasets_per_contrib = Counter()

    def dataset_id(record_no):
        datasets_per_contrib[record_no] += 1
        dataset_count = datasets_per_contrib[record_no]
        return f'{record_no}-{dataset_count}'

    # XXX: how idempotent is this?
    return [
        {
            'ID': dataset_id(stats['record_no']),
            'Contribution_ID': stats['record_no'],
            'Module': stats['module'],
            'Language_Count': stats['lang_count'],
            'Glottocode_Count': stats['glottocode_count'],
            'Parameter_Count': stats['parameter_count'],
            'Value_Count': stats['value_count'],
            'Form_Count': stats['form_count'],
            'Entry_Count': stats['entry_count'],
            'Example_Count': stats['example_count'],
        }
        for stats in dataset_stats]


def dataset_languages_from_dataset_stats(dataset_stats, datasets):
    return [
        {
            'ID': '{}-{}'.format(ds['ID'], lid),
            'Language_ID': lid,
            'Dataset_ID': ds['ID'],
            # 'Parameter_Count': stats['glottocode_features'].get(lid, 0),
            'Value_Count': stats['glottocode_values'].get(lid, 0),
            'Form_Count': stats['glottocode_forms'].get(lid, 0),
            'Entry_Count': stats['glottocode_entries'].get(lid, 0),
            'Example_Count': stats['glottocode_examples'].get(lid, 0),
        }
        for ds, stats in zip(datasets, dataset_stats)
        for lid in stats['langs']]


def contributions_from_records(records, datasets):
    contribution_ids = {ds['Contribution_ID'] for ds in datasets}
    return [
        {
            'ID': rec['id'],
            'Name': rec['title'],
            'Description': rec['description'],
            'Version': rec['version'],
            'Creators': [
                c['name'] for c in rec['creators']],
            'Contributors': [
                c['name'] for c in rec.get('contributors', ())],
            'DOI': rec['doi'],
            'Concept_DOI': rec['conceptdoi'],
            'Concept_ID': rec['conceptid'],
            'GitHub_Link': rec.get('git-link'),
            'Date_Created': rec['created'],
            'Date_Updated': rec['updated'],
            # TODO: Communities are not extracted from the zenodo response
            'Communities': [
                c['id'] for c in rec.get('communities', ())],
            'License': rec['license'],
            'Zenodo_ID': rec['id'],
            'Zenodo_Link': 'https://zenodo.org/records/{}'.format(
                rec['id']),
            'Zenodo_Keywords': rec.get('keywords', ()),
            'Zenodo_Type': rec['resource_type'],
        }
        for rec in records
        if rec['id'] in contribution_ids]


class Dataset(BaseDataset):
    dir = Path(__file__).parent
    id = "cldf_meta"

    def cldf_specs(self):  # A dataset must declare all CLDF sets it creates.
        return CLDFSpec(
            dir=self.cldf_dir,
            module='Generic',
            metadata_fname='cldf-metadata.json')

    def cmd_download(self, args):
        """
        Download files to the raw/ directory. You can use helpers methods of `self.raw_dir`, e.g.

        >>> self.raw_dir.download(url, fname)
        """
        access_token = dl.retrieve_access_token()

        try:
            records = self.raw_dir.read_json('zenodo-metadata.json')['records']
        except IOError:
            args.log.error(
                'No zenodo metadata found.'
                '  Run `cldfbench cldf-meta.updatemd cldfbench_cldf_meta.py`'
                '  to download the metadata.')
            return

        files_without_cldf = {
            (record_no, file)
            for record_no, file, _ in islice(
                self.etc_dir.read_csv('not-cldf.csv'), 1, None)}

        # TODO: add 'All Versions' DOI for the meta database itself, once we have one.
        with open(self.etc_dir / 'blacklist.csv', encoding='utf-8') as f:
            rdr = csv.reader(f)
            blacklist = {doi for doi, _ in islice(rdr, 1, None) if doi}

        data_dir = self.raw_dir / 'datasets'

        records = (
            record
            for record in records
            if not is_blacklisted(blacklist, record))
        downloads = (
            Download(
                url=file['url'],
                destination=download_path(
                    data_dir, str(rec['id']), file['file_path']),
                checksum=file['checksum'])
            for rec in records
            for file in rec.get('files', ())
            # XXX what if someone sends a tarball?
            if might_be_zip(file)
            and (str(rec['id']), file['file_path']) not in files_without_cldf)
        downloads = [
            download
            for download in downloads
            if not download.destination.exists()]

        if downloads:
            print(
                'downloading', len(downloads), 'datasets...',
                file=sys.stderr, flush=True)
            download_datasets(downloads, access_token)
        else:
            print(
                'Datasets already up-to-date.',
                file=sys.stderr, flush=True)

    def cmd_makecldf(self, args):
        """
        Convert the raw data to a CLDF dataset.

        >>> args.writer.objects['LanguageTable'].append(...)
        """
        # Prepare metadata

        not_cldf_full = [
            CLDFError(*row)
            for row in islice(self.etc_dir.read_csv('not-cldf.csv'), 1, None)]

        with open(self.etc_dir / 'blacklist.csv', encoding='utf-8') as f:
            rdr = csv.reader(f)
            blacklist = {doi for doi, _ in islice(rdr, 1, None) if doi}

        try:
            records = self.raw_dir.read_json('zenodo-metadata.json')['records']
            records = [
                record
                for record in records
                if not is_blacklisted(blacklist, record)]
        except IOError:
            args.log.error(
                'No zenodo metadata found.'
                '  Run `cldfbench cldf-meta.updatemd cldfbench_cldf_meta.py`'
                '  to download the metadata.')
            return

        # Read CLDF data

        print('finding cldf datasets..', file=sys.stderr, flush=True)
        not_cldf = {(err.record_no, err.file) for err in not_cldf_full}
        data_dir = self.raw_dir / 'datasets'
        data_archives = [
            DataArchive(
                record_no=rec['id'],
                file_id=file['file_path'],
                path=download_path(data_dir, str(rec['id']), file['file_path']))
            for rec in records
            for file in rec.get('files', ())
            if might_be_zip(file)
            and (str(rec['id']), file['file_path']) not in not_cldf]

        missing_files = [
            archive
            for archive in data_archives
            if not archive.path.is_file()]
        if missing_files:
            print(
                '\n'.join(
                    f'{archive.record_no}:{archive.file_id}: file not found'
                    for archive in missing_files),
                file=sys.stderr)
            print(
                'ERROR: Some datasets seem to be missing in raw/.',
                'You might have to re-run `cldfbench download`.',
                sep='\n', file=sys.stderr, flush=True)
            return

        print(
            'extracting databases from', len(data_archives), 'zip files...',
            file=sys.stderr, flush=True)
        cldf_errors = ErrorFilter()
        with Pool() as pool:
            dataset_stats = list(cldf_errors.filter(
                (stats, err)
                for chunk in loggable_progress(
                    pool.imap(stats_from_zip, data_archives))
                for stats, err in chunk))
        if cldf_errors.errors:
            print(
                '\n'.join(
                    f'{err.record_no}:{err.file}: no cldf data found'
                    for err in cldf_errors.errors),
                file=sys.stderr)
            not_cldf_full.extend(cldf_errors.errors)
            not_cldf_full.sort(key=lambda err: int(err.record_no))
            not_cldf_path = self.etc_dir / 'not-cldf.csv'
            with open(not_cldf_path, 'w', encoding='utf-8') as f:
                wtr = csv.writer(f)
                wtr.writerow(CLDFError._fields)
                wtr.writerows(not_cldf_full)

        print(
            'loading language info from glottolog...',
            file=sys.stderr, flush=True)
        by_glottocode = {lg.id: lg for lg in args.glottolog.api.languoids()}
        by_isocode = {lg.iso: lg for lg in by_glottocode.values() if lg.iso}

        dataset_stats = [
            raw_stats_to_glottocode_stats(stats, by_glottocode, by_isocode)
            for stats in dataset_stats]

        # Create CLDF tables

        print('assembling language table...', file=sys.stderr, flush=True)
        languages = languages_from_dataset_stats(dataset_stats, by_glottocode)

        # TODO count all teh things! o/

        print('assembling dataset tables...', file=sys.stderr, flush=True)

        datasets = datasets_from_dataset_stats(dataset_stats)
        dataset_languages = dataset_languages_from_dataset_stats(
            dataset_stats, datasets)
        contributions = contributions_from_records(records, datasets)
        # just checking my assumptions
        assert all(
            row['Concept_DOI'].split('.')[-1] == row['Concept_ID']
            for row in contributions)

        # Write CLDF data

        print('writing cldf data...', file=sys.stderr, flush=True)

        args.writer.cldf.add_component('LanguageTable')

        args.writer.cldf.add_component(
            'ContributionTable',
            'Version',
            {'name': 'Creators', 'separator': ' ; '},
            {'name': 'Contributors', 'separator': ' ; '},
            'DOI',
            'Concept_DOI',
            'Concept_ID',
            'Date',
            {'name': 'Communities', 'separator': ';'},
            'License',
            'Zenodo_Link',
            'Zenodo_ID',
            {'name': 'Zenodo_Keyword', 'separator': ';'},
            'Zenodo_Type',
            'GitHub_Link')

        args.writer.cldf.add_table(
            'datasets.csv',
            'http://cldf.clld.org/v1.0/terms.rdf#id',
            'http://cldf.clld.org/v1.0/terms.rdf#contributionReference',
            'Module',
            {'name': 'Language_Count', 'datatype': 'integer'},
            {'name': 'Glottocode_Count', 'datatype': 'integer'},
            {'name': 'Parameter_Count', 'datatype': 'integer'},
            {'name': 'Value_Count', 'datatype': 'integer'},
            {'name': 'Form_Count', 'datatype': 'integer'},
            {'name': 'Entry_Count', 'datatype': 'integer'},
            {'name': 'Example_Count', 'datatype': 'integer'})

        args.writer.cldf.add_table(
            'dataset-languages.csv',
            'http://cldf.clld.org/v1.0/terms.rdf#id',
            'Dataset_ID',
            'http://cldf.clld.org/v1.0/terms.rdf#languageReference',
            # {'name': 'Parameter_Count', 'datatype': 'integer'},
            {'name': 'Value_Count', 'datatype': 'integer'},
            {'name': 'Form_Count', 'datatype': 'integer'},
            {'name': 'Entry_Count', 'datatype': 'integer'},
            {'name': 'Example_Count', 'datatype': 'integer'})
        args.writer.cldf.add_foreign_key(
            'dataset-languages.csv', 'Dataset_ID',
            'datasets.csv', 'ID')

        args.writer.objects['LanguageTable'] = languages
        args.writer.objects['ContributionTable'] = contributions
        args.writer.objects['datasets.csv'] = datasets
        args.writer.objects['dataset-languages.csv'] = dataset_languages

    def cmd_readme(self, args):
        autogenerated = super().cmd_readme(args)
        snippet_path = self.etc_dir / 'readme-snippet.md'
        custom = snippet_path.read_text(encoding='utf-8')
        return f'{autogenerated}\n{custom}'
