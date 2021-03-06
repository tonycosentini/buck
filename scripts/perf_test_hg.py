#!/usr/bin/env python
"""Performance test to compare the performance of buck between two revisions.

The general algorithm is:

Checkout <revisions_to_go_back - 1>
Warm up the cache:
  Set .buckversion to old revision, build all targets
  Set .buckversion to new revision, build all targets

For each revision to test:
  - Rename directory being tested
  - Build all targets, check to ensure everything pulls from dir cache
  - Check out revision to test
  - Clean Build all targets <iterations_per_diff> times, only reading from
      cache, not writing (except for the last one, write that time)
  - Buck build all targets to verify no-op build works.

"""
import argparse
import re
import subprocess
import os
import tempfile
import sys

from collections import defaultdict
from datetime import datetime


def createArgParser():
    parser = argparse.ArgumentParser(
        description='Run the buck performance test')
    parser.add_argument(
        '--perftest_id',
        action='store',
        type=str,
        help='The identifier of this performance test')
    parser.add_argument(
        '--revisions_to_go_back',
        action='store',
        type=int,
        help='The maximum number of revisions to go back when testing')
    parser.add_argument(
        '--iterations_per_diff',
        action='store',
        type=int,
        help='The number of iterations to run on diff')
    parser.add_argument(
        '--targets_to_build',
        action='append',
        type=str,
        help='The targets to build')
    parser.add_argument(
        '--repo_under_test',
        action='store',
        type=str,
        help='Path to the repo under test')
    parser.add_argument(
        '--project_under_test',
        action='store',
        type=str,
        help='Path to the project folder being tested under repo')
    parser.add_argument(
        '--path_to_buck',
        action='store',
        type=str,
        help='The path to the buck binary')
    parser.add_argument(
        '--old_buck_revision',
        action='store',
        type=str,
        help='The original buck revision')
    parser.add_argument(
        '--new_buck_revision',
        action='store',
        type=str,
        help='The new buck revision')
    return parser


def log(message):
    print '%s\t%s' % (str(datetime.now()), message)
    sys.stdout.flush()


def timedelta_total_seconds(timedelta):
    return (
        timedelta.microseconds + 0.0 +
        (timedelta.seconds + timedelta.days * 24 * 3600) * 10 ** 6) / 10 ** 6


class BuildResult():
    def __init__(self, time_delta, cache_results, rule_key_map):
        self.time_delta = time_delta
        self.cache_results = cache_results
        self.rule_key_map = rule_key_map


def clean(cwd):
    log('Running hg purge.')
    subprocess.check_call(
        ['hg', 'purge', '--all'],
        cwd=cwd)


def reset(revision, cwd):
    subprocess.check_call(
        ['hg', 'revert', '-a', '-r', revision],
        cwd=cwd)


def buck_clean(args, cwd):
    log('Running buck clean.')
    subprocess.check_call(
        [args.path_to_buck, 'clean'],
        cwd=cwd)


def get_revisions(args):
    cmd = ['hg', 'log',
            '--limit', str(args.revisions_to_go_back + 1),
            '-T', '{node}\\n',
            # only look for changes under specific folder
            args.project_under_test
            ]
    proc = subprocess.Popen(
        cmd,
        cwd=args.repo_under_test,
        stdout=subprocess.PIPE)
    try:
        return list(reversed(proc.communicate()[0].splitlines()))
    finally:
        if proc.wait():
            raise subprocess.CalledProcessError(
                proc.returncode,
                ' '.join(cmd))


def checkout(revision, cwd):
    log('Checking out %s.' % revision)
    subprocess.check_call(
        ['hg', 'update', '--clean', revision],
        cwd=cwd)


BUILD_RESULT_LOG_LINE = re.compile(
    r'BuildRuleFinished\((?P<rule_name>[\w_\-:#\/,]+)\): (?P<result>[A-Z_]+) '
    r'(?P<cache_result>[A-Z_]+) (?P<success_type>[A-Z_]+) '
    r'(?P<rule_key>[0-9a-f]*)')


RULEKEY_LINE = re.compile(
    r'^INFO: RuleKey (?P<rule_key>[0-9a-f]*)='
    r'(?P<rule_key_debug>.*)$')


BUCK_LOG_RULEKEY_LINE = re.compile(
    r'.*\[[\w ]+\](?:\[command:[0-9a-f-]+\])?\[tid:\d+\]'
    r'\[com.facebook.buck.rules.RuleKey[\$\.]?Builder\] '
    r'RuleKey (?P<rule_key>[0-9a-f]+)='
    r'(?P<rule_key_debug>.*)$')


def buck_build_target(args, cwd, targets, perftest_side, log_as_perftest=True):
    """Builds a target with buck and returns performance information.
    """
    log('Running buck build %s.' % ' '.join(targets))
    bucklogging_properties_path = os.path.join(
        cwd, '.bucklogging.local.properties')
    with open(bucklogging_properties_path, 'w') as bucklogging_properties:
        # The default configuration has the root logger and FileHandler
        # discard anything below FINE level.
        #
        # We need RuleKey logging, which uses FINER (verbose), so the
        # root logger and file handler both need to be reconfigured
        # to enable verbose logging.
        bucklogging_properties.write(
            '''.level=FINER
            java.util.logging.FileHandler.level=FINER''')
    env = os.environ.copy()
    # Force buck to pretend it's repo is clean.
    env.update({
        'BUCK_REPOSITORY_DIRTY': '0'
    })
    if log_as_perftest:
        env.update({
            'BUCK_EXTRA_JAVA_ARGS':
            '-Dbuck.perftest_id=%s, -Dbuck.perftest_side=%s' % (
            args.perftest_id, perftest_side)
        })
    start = datetime.now()
    tmpFile = tempfile.TemporaryFile()
    try:
        subprocess.check_call(
            [args.path_to_buck, 'build', '--deep'] + targets + ['-v', '5'],
            stdout=tmpFile,
            stderr=tmpFile,
            cwd=cwd,
            env=env)
    except:
        tmpFile.seek(0)
        log('Buck build failed: %s' % tmpFile.read())
        raise
    tmpFile.seek(0)
    finish = datetime.now()

    java_utils_log_path = os.path.join(
        cwd,
        'buck-out', 'log', 'buck-0.log')
    if os.path.exists(java_utils_log_path):
        pattern = BUCK_LOG_RULEKEY_LINE
        build_output_file = open(java_utils_log_path)
    else:
        pattern = RULEKEY_LINE
        build_output_file = tmpFile

    rule_debug_map = {}
    for line in build_output_file:
        match = pattern.match(line)
        if match:
            rule_debug_map[match.group('rule_key')] = match.group(
                'rule_key_debug')

    logfile_path = os.path.join(
        cwd,
        'buck-out', 'bin', 'build.log')
    cache_results = defaultdict(list)
    rule_key_map = {}
    with open(logfile_path, 'r') as logfile:
        for line in logfile.readlines():
            line = line.strip()
            match = BUILD_RESULT_LOG_LINE.search(line)
            if match:
                rule_name = match.group('rule_name')
                rule_key = match.group('rule_key')
                if not rule_key in rule_debug_map:
                    raise Exception('''ERROR: build.log contains an entry
                        which was not found in buck build -v 5 output.
                        Rule: {0}, rule key: {1}'''.format(rule_name, rule_key))
                cache_results[match.group('cache_result')].append({
                    'rule_name': rule_name,
                    'rule_key': rule_key,
                    'rule_key_debug': rule_debug_map[rule_key]
                })
                rule_key_map[match.group('rule_name')] = (rule_key, rule_debug_map[rule_key])

    result = BuildResult(finish - start, cache_results, rule_key_map)
    cache_counts = {}
    for key, value in result.cache_results.iteritems():
        cache_counts[key] = len(value)
    log('Test Build Finished! Elapsed Seconds: %d, Cache Counts: %s' % (
        timedelta_total_seconds(result.time_delta), repr(cache_counts)))
    return result


def set_perftest_side(
        args,
        cwd,
        perftest_side,
        cache_mode,
        dir_cache_only=True):
    log('Reconfiguring to test %s version of buck.' % perftest_side)
    buckconfig_path = os.path.join(cwd, '.buckconfig.local')
    with open(buckconfig_path, 'w') as buckconfig:
        buckconfig.write('''[cache]
    %s
    dir = buck-cache-%s
    dir_mode = %s
  ''' % ('mode = dir' if dir_cache_only else '', perftest_side, cache_mode))
        buckconfig.truncate()
    buckversion_path = os.path.join(cwd, '.buckversion')
    with open(buckversion_path, 'w') as buckversion:
        if perftest_side == 'old':
            buckversion.write(args.old_buck_revision + os.linesep)
        else:
            buckversion.write(args.new_buck_revision + os.linesep)
        buckversion.truncate()


def build_all_targets(
        args,
        cwd,
        perftest_side,
        cache_mode,
        run_clean=True,
        dir_cache_only=True,
        log_as_perftest=True):
    set_perftest_side(
        args,
        cwd,
        perftest_side,
        cache_mode,
        dir_cache_only=dir_cache_only)
    targets = []
    for target_str in args.targets_to_build:
        targets.extend(target_str.split(','))
    if run_clean:
        buck_clean(args, cwd)
    #TODO(rowillia): Do smart things with the results here.
    return buck_build_target(
        args,
        cwd,
        targets,
        perftest_side,
        log_as_perftest=log_as_perftest)


def run_tests_for_diff(args, revisions_to_test, test_index, last_result):
    log('=== Running tests at revision %s ===' % revisions_to_test[test_index])
    new_directory_name = (os.path.basename(args.repo_under_test) +
                          '_test_iteration_%d' % test_index)

    # Rename the directory to flesh out any cache problems.
    cwd_root = os.path.join(os.path.dirname(args.repo_under_test),
                       new_directory_name)
    cwd = os.path.join(cwd_root, args.project_under_test)

    log('Renaming %s to %s' % (args.repo_under_test, cwd_root))
    os.rename(args.repo_under_test, cwd_root)

    try:
        log('== Checking new revision for problems with absolute paths ==')
        result = build_all_targets(args, cwd, 'new', 'readonly')
        suspect_keys = [
            x
            for x in result.cache_results.keys()
            if x not in ['DIR_HIT', 'IGNORED']
        ]
        if suspect_keys:
            log('Building at revision %s with the new buck version '
                'was unable to reuse the cache from a previous run.  '
                'This suggests one of the rule keys contains an '
                'absolute path.' % (
                    revisions_to_test[test_index - 1]))
            for rule in result.cache_results['MISS']:
                rule_name = rule['rule_name']
                key, key_debug = result.rule_key_map[rule_name]
                old_key, old_key_debug = last_result.rule_key_map[rule_name]
                log('Rule %s missed.' % rule_name)
                log('\tOld Rule Key (%s): %s.' % (old_key, old_key_debug))
                log('\tNew Rule Key (%s): %s.' % (key, key_debug))
            raise Exception('Failed to reuse cache across directories!!!')

        checkout(revisions_to_test[test_index], cwd_root)

        for attempt in xrange(args.iterations_per_diff):
            cache_mode = 'readonly'
            if attempt == args.iterations_per_diff - 1:
                cache_mode = 'readwrite'

            build_all_targets(args, cwd, 'old', cache_mode)
            build_all_targets(args, cwd, 'new', cache_mode)

        log('== Checking new revision to ensure noop build does nothing. ==')
        result = build_all_targets(
            args,
            cwd,
            'new',
            cache_mode,
            run_clean=False)
        if (len(result.cache_results.keys()) != 1 or
                'LOCAL_KEY_UNCHANGED_HIT' not in result.cache_results):
            result.cache_results.pop('DIR_HIT', None)
            raise Exception(
                'Doing a noop build at revision %s with the new '
                'buck version did not hit all of it\'s keys.\nMissed '
                'Rules: %s' % (
                    revisions_to_test[test_index - 1],
                    repr(result.cache_results)))

    finally:
        log('Renaming %s to %s' % (cwd_root, args.repo_under_test))
        os.rename(cwd_root, args.repo_under_test)

    return result


def main():
    args = createArgParser().parse_args()
    log('Running Performance Test!')
    clean(args.repo_under_test)
    revisions_to_test = get_revisions(args)
    log('Found revisions to test: %d' % len(revisions_to_test))
    log('\n'.join(revisions_to_test))
    # Checkout the revision previous to the test and warm up the local dir
    # cache.
    log('=== Warming up cache ===')
    checkout(revisions_to_test[0], args.repo_under_test)
    cwd = os.path.join(args.repo_under_test, args.project_under_test)
    # build with different variations to warm up cache and work around
    # cache weirdness
    build_all_targets(
        args,
        cwd,
        'old',
        'readwrite',
        dir_cache_only=False,
        log_as_perftest=False)
    build_all_targets(
        args,
        cwd,
        'old',
        'readwrite',
        log_as_perftest=False)
    build_all_targets(
        args,
        cwd,
        'new',
        'readwrite',
        dir_cache_only=False,
        log_as_perftest=False)
    results_for_new = build_all_targets(
        args,
        cwd,
        'new',
        'readwrite',
        log_as_perftest=False)
    log('=== Cache Warm!  Running tests ===')
    for i in xrange(1, args.revisions_to_go_back):
        results_for_new = run_tests_for_diff(
            args,
            revisions_to_test,
            i,
            results_for_new)


if __name__ == '__main__':
    main()
