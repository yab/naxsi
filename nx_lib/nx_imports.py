import urlparse
import itertools
import datetime
import time
import pprint
import gzip
import glob
import logging
import sys
#from nx_lib.nx_filter import NxFilter
from select import select
import re


class NxImportFilter():
    """ Used to handle user supplied input filters on data acquisition """
    def __init__(self, filters):
        self.gi = None
        self.res_op = []
        self.kw = {
            "ip" : {"methods" : "=,!=,=~"},
            "date" : {"methods" : "=,!=,=~,>,<"},
            "server" : {"methods" : "=,!=,=~"},
            "uri" : {"methods" : "=,!=,=~"},
            "zone" : {"methods" : "=,!="},
            "var_name" : {"methods" : "=,!=,=~"},
            "content" : {"methods" : "=,!=,=~"},
            "country" : {"methods" : "=,!="}
            }
        try:
            import GeoIP
            self.gi = GeoIP.new(GeoIP.GEOIP_MEMORY_CACHE)
            print "oh ay !"
        except:
            print """Python's GeoIP module is not present.
            'World Map' reports won't work,
            and you can't use per-country filters."""
    def word(self, w, res):
        if w not in self.kw.keys():
            return -1
        res.append(w)
        return 1

    def check(self, w, res):
        if w not in self.kw[res[-1]]["methods"].split(","):
            print "operator "+w+" not allowed for var "+res[-1]
            return -1
        res.append(w)
        return 2

    def checkval(self, w, res):
        res.append(w)
        return 3

    def synt(self, w, res):
        if w != "or" and w != "and":
            return -1
        res.append(w)
        return 0

    def filter_build(self, instr):
        words = instr.split(' ')
        res_op = []
        # -1 : err, 0 : var, 1 : check, 2 : syntax (and/or), 3 : value
        state = 0
        for w in words:
            if state == 0:
                state = self.word(w, res_op)
            elif state == 1:
                state = self.check(w, res_op)
            elif state == 2:
                state = self.checkval(w, res_op)
            elif state == 3:
                state = self.synt(w, res_op)
            if state == -1:
                print "Unable to build filter, check you syntax at '"+w+"'"
                return False

        self.res_op = res_op
        return True

    def subfil(self, src, sub):
        if sub[0] not in src:
            print "Unable to filter : key "+sub[0]+" does not exist in dict"
            return False
        srcval = src[sub[0]]
        filval = sub[2]
        if sub[1] == "=" and srcval == filval:
            return True
        elif sub[1] == "!=" and srcval != filval:
            return True
        elif sub[1] == "=~" and re.match(filval, srcval):
            return True
        return False

    def dofilter(self, src):
        filters = self.res_op
        if self.gi is not None:
            src['country'] = self.gi.country_code_by_addr(src['ip'])
        else:
            src['country'] = "??"
        last = False
        ok_fail = False
        while last is False:
            sub = filters[0:3]
            filters = filters[3:]
            if len(filters) == 0:
                last = True
#            print "test vs:"+str(sub)+"",
            result = self.subfil(src, sub)
 #           print "==>"+str(result)
            # Final check
            if last is True:
                # if last keyword was or, we can have a fail on last test
                # and still return true.
                if ok_fail is True:
                    return True
                return result
            # if this test succeed with a OR, we can fail next.
            if result is True and filters[0] == "or":
                return True
            if result is False and filters[0] == "and":
                return False
            # remove and/or
            filters = filters[1:]
            ok_fail = False
        return True
        
class NxReader():
    """ Feeds the given injector from logfiles """
    def __init__(self, injector, stdin=False, lglob=[], step=50,
                 stdin_timeout=5, date_filters=[["", ""]]):
        self.injector = injector
        self.step = step
        self.files = []
        self.date_filters = date_filters
        self.timeout = stdin_timeout
        self.stdin = False
        if stdin is not False:
            print "Using stdin."
            self.stdin = True
            return
        if len(lglob) > 0:
            for regex in lglob:
                self.files.extend(glob.glob(regex))
        print "List of imported files :"+str(self.files)

    def read_stdin(self):
        rlist, _, _ = select([sys.stdin], [], [], self.timeout)
        if rlist:
            s = sys.stdin.readline()
            if s == '':
                return False
            self.injector.acquire_nxline(s)
            return True
        else:
            return False
    def read_files(self):
        if self.stdin is True:
            ret = ""
            while self.read_stdin() is True:
                pass
            self.injector.commit()
            print "Committing to db ..."
            self.injector.wrapper.StopInsert()
            return 0
        count = 0
        total = 0
        for lfile in self.files:
            success = not_nx = discard = malformed = 0
            print "Importing file "+lfile
            try:
                if lfile.endswith(".gz"):
                    fd = gzip.open(lfile, "rb")
                else:
                    fd = open(lfile, "r")
            except:
                print "Unable to open file : "+lfile
                return 1
            for line in fd:
                ret = self.injector.acquire_nxline(line)
                if ret == 0:
                    success += 1
                    count += 1
                elif ret == 1:
                    discard += 1
                elif ret == 2:
                    not_nx += 1
                elif ret == -1:
                    malformed += 1
                if count == self.step:
                    self.injector.commit()
                    count = 0
            fd.close()
            print "\tSuccessful events :"+str(success)
            print "\tFiltered out events :"+str(discard)
            print "\tNon-naxsi lines :"+str(not_nx)
            print "\tMalformed/incomplete lines "+str(malformed)
            total += success
        if count > 0:
            self.injector.commit()
            print "End of db commit... "
            self.injector.wrapper.StopInsert()
        print "Count (lines) success:"+str(total)
        return 0

class NxInject():
    """ Transforms naxsi error log into dicts """
    # din_fmt and fil_fmt are format of dates from logs and from user-supplied filters
    def __init__(self, wrapper, filters=""):
        self.naxsi_keywords = [" NAXSI_FMT: ", " NAXSI_EXLOG: "]
        self.wrapper = wrapper
        self.dict_buf = []
        self.total_objs = 0
        self.total_commits = 0
        self.filters = filters
        self.filt_engine = None
        
        if self.filters is not None:
            self.filt_engine = NxImportFilter(self.filters)
            if self.filt_engine.filter_build(self.filters) is False:
                print "Unable to create filter, abort."
                sys.exit(-1)

    def commit(self):
        """Process dicts of dict (yes) and push them to DB """
        self.total_objs += len(self.dict_buf)
        count = 0
        for entry in self.dict_buf:
            if not entry.has_key('uri'):
                entry['uri'] = ''
            if not entry.has_key('server'):
                entry['server'] = ''
            url_id = self.wrapper.insert(url = entry['uri'], table='urls')()
            if not entry.has_key('content'):
                entry['content'] = ''
            # NAXSI_EXLOG lines only have one triple (zone,id,var_name), but has non-empty content
            if 'zone' in entry.keys():
                count += 1
                if 'var_name' not in entry.keys():
                    entry['var_name'] = ''
                    #try:
                exception_id = self.wrapper.insert(zone=entry['zone'], var_name=entry['var_name'], rule_id=entry['id'], content=entry['content'], table='exceptions')()
                self.wrapper.insert(peer_ip=entry['ip'], host = entry['server'], url_id=str(url_id), id_exception=str(exception_id),
                                    date=str(entry['date']), table = 'connections')()#[1].force_commit()
                # except:
                #     print "Unable to insert (EXLOG) entry (malformed ?)"
                #     pprint.pprint(entry)
                    
            # NAXSI_FMT can have many (zone,id,var_name), but does not have content
            # we iterate over triples.
            elif 'zone0' in entry.keys():
                count += 1
                for i in itertools.count():
                    commit = True
                    zn = ''
                    vn = ''
                    rn = ''
                    if 'var_name' + str(i) in entry.keys():
                        vn = entry['var_name' + str(i)]
                    if 'zone' + str(i) in entry.keys():
                        zn  = entry['zone' + str(i)]
                    else:
                        commit = False
                        break
                    if 'id' + str(i) in entry.keys():
                        rn = entry['id' + str(i)]
                    else:
                        commit = False
                        break
                    if commit is True:
                        exception_id = self.wrapper.insert(zone = zn, var_name = vn, rule_id = rn, content = '', table = 'exceptions')()
                        self.wrapper.insert(peer_ip=entry['ip'], host = entry['server'], url_id=str(url_id), id_exception=str(exception_id),
                                            date=str(entry['date']), table = 'connections')()
                    else:
                        print "Malformed line."
                        count -= 1
        self.total_commits += count
        # Real clearing of dict.
        del self.dict_buf[0:len(self.dict_buf)]
    def exception_to_dict(self, line):
        """Parses a naxsi exception to a dict, 
        1 on error, 0 on success"""
        odict = urlparse.parse_qs(line)
        for x in odict.keys():
            odict[x][0] = odict[x][0].replace('\n', "\\n")
            odict[x][0] = odict[x][0].replace('\r', "\\r")
            odict[x] = odict[x][0]
        # check for incomplete/truncated lines
        if 'zone0' in odict.keys():
            for i in itertools.count():
                is_z = is_id = False
                if 'zone' + str(i) in odict.keys():
                    is_z = True
                if 'id' + str(i) in odict.keys():
                    is_id = True
                if is_z is True and is_id is True:
                    continue
                if is_z is False and is_id is False:
                    break
#                if is_z is True:
                try:
                    del (odict['zone' + str(i)])
                #if is_id is True:
                    del (odict['id' + str(i)])
                    del (odict['var_name' + str(i)])
                except:
                    pass
                break
                    
        return odict
    def date_unify(self, date):
        idx = 0
        res = ""
        supported_formats = [
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            #2013-01-30T15:16:53+01:00
            "%Y-%m-%dT%H:%M:%S+",
            ]
        while date[idx].isdigit() is False:
            idx += 1
        valid_datechars = ["/", " ", ":", "-", "T"]
        while idx < len(date):
            if date[idx].isdigit():
                pass
            elif date[idx] in valid_datechars:
                pass
            else:
                break
            # hack for "2013-01-31T02:11:12+01:00" formats
            if date[idx] == "T":
                res = res+" "
            else:
                res = res+date[idx]
            idx += 1
        return res.replace("/", "-")
    
            
            
    # can return : 
    # 0 : ok
    # 1 : ok, but discarded by filters
    # -1 : incomplete/malformed line 
    # 2 : not naxsi line
    def acquire_nxline(self, line, date_format='%Y/%m/%d %H:%M:%S',
                       sod_marker=[' [error] ', ' [debug] '], eod_marker=[', client: ', '']):
        line = line.rstrip('\n')
        for mark in sod_marker:
            date_end = line.find(mark)
            if date_end != -1:
                break
        for mark in eod_marker:
            if mark == '':
                data_end = len(line)
                break
            data_end = line.find(mark)
            if data_end != -1:
                break
        if date_end == -1 or data_end == 1:
            return -1
        date = self.date_unify(line[:date_end])
#        try:
#        time.strptime(date.replace("-", "/"), date_format)
 #       except:
  #          print "Unable to parse date '"+date+"'"
            
        chunk = line[date_end:data_end]
        md = None
        for word in self.naxsi_keywords:
            idx = chunk.find(word)
            if (idx != -1):
                md = self.exception_to_dict(chunk[idx+len(word):])
                if md is None:
                    return -1
                md['date'] = date
                break
        if md is None:
            return 2
        # if input filters on country were used, forced geoip XXX
        if self.filt_engine is None or self.filt_engine.dofilter(md) is True:
            self.dict_buf.append(md)
            return 0
        return 1
