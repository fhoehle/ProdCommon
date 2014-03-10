#!/usr/bin/env python
"""
BossLite PBS/torque interface

dave.newbold@cern.ch, June 09

rewritten:
andrew.m.melo@vanderbilt.edu, Mar 12

"""

__revision__ = "$Id: SchedulerPbs.py,v 1.4 2011/09/05 18:00:16 mcinquil Exp $"
__version__ = "$Revision: 1.4 $"

import re, os, time, uuid
import tempfile, os.path
import subprocess, re, socket
import shutil, stat, time

from ProdCommon.BossLite.Scheduler.SchedulerInterface import SchedulerInterface
from ProdCommon.BossLite.Common.Exceptions import SchedulerError
from ProdCommon.BossLite.DbObjects.Job import Job
from ProdCommon.BossLite.DbObjects.Task import Task
from ProdCommon.BossLite.DbObjects.RunningJob import RunningJob

class SchedulerPbsv2 (SchedulerInterface) :
    """
    basic class to handle pbs jobs
    """
    def __init__( self, **args):
        super(SchedulerPbsv2, self).__init__(**args)
        self.jobScriptDir      = args['jobScriptDir']
        self.jobResDir         = args['jobResDir']
        self.queue             = args['queue']
        self.workerNodeWorkDir = args.get('workernodebase', '')
        self.hostname          = args.get('hostname', None)
        if not self.hostname:
            self.hostname = socket.gethostname()
        self.resources         = args.get('resources', '')
        self.use_proxy         = args.get('use_proxy', True)
        self.group_list        = args.get('grouplist', '')
        self.forceTransferFiles= args.get('forcetransferfiles', 0)

        self.res_dict       = {}
        self.proxy_location = os.environ.get( 'X509_USER_PROXY', \
                                              '/tmp/x509up_u'+ repr(os.getuid()) )

        self.status_map={'E':'R',
                         'H':'SS',
                         'Q':'SS',
                         'R':'Running',
                         'S':'R',
                         'T':'R',
                         'W':'SS',
                         'Done':'SD',
                         'C':'SD'}

    def jobDescription ( self, obj, requirements='', config='', service = '' ):
        """
        retrieve scheduler specific job description
        return it as a string
        """
        raise NotImplementedError

    def submit ( self, obj, requirements='', config='', service = '' ) :
        """
        set up submission parameters and submit

        return jobAttributes, bulkId, service

        - jobAttributs is a map of the format
              jobAttributes[ 'name' : 'schedulerId' ]
        - bulkId is an eventual bulk submission identifier
        - service is a endpoit to connect withs (such as the WMS)
        """
        
        if type(obj) == RunningJob or type(obj) == Job:
            map, taskId, queue = self.submitJob(obj, requirements)
        elif type(obj) == Task :
            map, taskId, queue = self.submitTask (obj, requirements ) 

        return map, taskId, queue

    def submitTask ( self, task, requirements=''):

        ret_map={}
        for job in task.getJobs() :
            map, taskId, queue = self.submitJob(job, task, requirements)
            ret_map.update(map)

        return ret_map, taskId, queue

    def submitJob ( self, job, task=None, requirements=''):
        """ Need to copy the inputsandbox to WN before submitting a job"""
        # Write a temporary submit script
        # NB: we assume an env var PBS_JOBCOOKIE points to the exec dir on the batch host

        inputFiles = task['globalSandbox'].split(',')
        pbsScript  = tempfile.NamedTemporaryFile()
        epilogue   = tempfile.NamedTemporaryFile( prefix = 'epilogue.' )
        if not self.workerNodeWorkDir:
            self.workerNodeWorkDir = os.path.join( os.getcwd(), 'CRAB-PBSV2' )
            if not os.path.exists( self.workerNodeWorkDir ):
                os.mkdir( self.workerNodeWorkDir )

        self.stageDir = os.path.join( os.getcwd(), 'CRAB-PBSV2' )
        if not os.path.exists( self.stageDir ):
            os.mkdir( self.stageDir )

        # Generate a UUID for transfering input files
        randomPrefix = uuid.uuid4().hex
        
        # Begin building PBS script
        s=[]
        s.append('#!/bin/sh')
        s.append('# This script generated by CRAB2 from http://cms.cern.ch')
        s.append('#PBS -e %s:%stmp_%s' % (self.hostname, self.jobResDir, job['standardError']) )
        s.append('#PBS -o %s:%stmp_%s' % (self.hostname, self.jobResDir, job['standardOutput']) )
        s.append('#PBS -N CMS_CRAB2')
        if self.resources:
            resourceList = self.resources.split(',')
            for resource in resourceList:
                s.append('#PBS -l %s' % resource)
        if self.group_list:
            s.append('#PBS -W group_list=%s' % self.group_list)

        if not self.forceTransferFiles:
            s.append('set -x')
        #s.append('ls -lah')
        #s.append('pwd')
        #s.append('set -x')
        #s.append('#PBS -T %s' % os.path.abspath(epilogue.name))

        # get files for stagein
        fileList = []
        inputFiles = task['globalSandbox'].split(',')

        # Do we want the proxy?
        if self.use_proxy:
            self.logging.debug("BossLite wants to use the proxy")
            if os.path.exists( self.proxy_location ):
                newProxyPath = "%sproxy.cert" % self.jobResDir
                shutil.copyfile( self.proxy_location, newProxyPath )
                self.logging.debug("Moved %s to %s" % \
                                        (self.proxy_location, newProxyPath))
                os.chmod(newProxyPath, stat.S_IRUSR | stat.S_IWUSR )
                inputFiles.append( newProxyPath )
            else:
                raise SchedulerError('Proxy Error',"Proxy not found at %s" % self.proxy_location)

      
        for file in inputFiles:
            targetFile = os.path.abspath( os.path.join( self.workerNodeWorkDir,
                                        "%s-%s" % (randomPrefix, os.path.basename( file ) ) ) )
            stageFile  = os.path.abspath( os.path.join( self.stageDir,
                                        "%s-%s" % (randomPrefix, os.path.basename( file ) ) ) )
            if self.forceTransferFiles:
                s.append('#PBS -W stagein=%s@%s:%s' % (targetFile, self.hostname, file))
            else:
                s.append('cp %s %s' % ( os.path.abspath(file), targetFile ) )

        #if fileList:
        #    s.append('#PBS -W stagein=%s' % ','.join(fileList))
        
        # Inform PBS of what we want to stage out
        fileList = []
        for file in job['outputFiles']:
            targetFile = os.path.abspath( os.path.join( self.workerNodeWorkDir,
                                       "%s-%s" % (randomPrefix, os.path.basename( file ) ) ) )
            stageFile  = os.path.abspath( os.path.join( task['outputDirectory'],
                                                        file ) ) 
            if self.forceTransferFiles:
                s.append('#PBS -W stageout=%s@%s:%s' % \
                    (targetFile,
                     self.hostname,
                     stageFile) )        # get out of $HOME
            #else:
            #    s.append('cp -f %s %s' % (targetFile, stageFile) )

        if self.forceTransferFiles:
            s.append('set -x')
        s.append('pwd')
        s.append('ls -lah')
        s.append('echo ***BEGINNING PBSV2***')
        s.append('CRAB2_OLD_DIRECTORY=`pwd`')
        s.append('CRAB2_PBS_WORKDIR=%s' % self.workerNodeWorkDir)
        if self.workerNodeWorkDir:
            s.append('cd %s' %  self.workerNodeWorkDir)


        s.append('CRAB2_WORKDIR=`pwd`/CRAB2-$PBS_JOBCOOKIE')
        s.append('if [ ! -d $CRAB2_WORKDIR ] ; then ')
        s.append('  mkdir -p $CRAB2_WORKDIR')
        s.append('fi')
        s.append('cd $CRAB2_WORKDIR')
        s.append('ls -lah')
        
        # move files up to $PBS_JOBCOOKIE
        inputFiles = task['globalSandbox'].split(',')

        for file in inputFiles:
            targetFile = "%s-%s" % (randomPrefix, os.path.basename( file ) )
            s.append('mv $CRAB2_PBS_WORKDIR/%s $CRAB2_WORKDIR/%s' % \
                            ( targetFile,
                              os.path.basename( file ) ) )
        
        # set proxy
        if self.use_proxy:
            s.append('mv $CRAB2_PBS_WORKDIR/%s-proxy.cert $CRAB2_WORKDIR/proxy.cert' % randomPrefix)
            s.append('export X509_USER_PROXY=$CRAB2_WORKDIR/proxy.cert')

        s.append("./%s %s" % (job['executable'], job['arguments']) )

        # move output files to where PBS can find them
        for file in job['outputFiles']:
            s.append('mv $CRAB2_WORKDIR/%s $CRAB2_PBS_WORKDIR/%s-%s' % (file, randomPrefix, file ) )
        
        fileList = []
        for file in job['outputFiles']:
            targetFile = os.path.abspath( os.path.join( self.workerNodeWorkDir,
                                       "%s-%s" % (randomPrefix, os.path.basename( file ) ) ) )
            stageFile  = os.path.abspath( os.path.join( task['outputDirectory'],
                                                        file ) ) 
            if not self.forceTransferFiles:
                s.append('mv -f %s %s' % (targetFile, stageFile) )


        s.append('cd $CRAB2_OLD_DIRECTORY')
        s.append('rm -rf $CRAB2_WORKDIR')
        pbsScript.write('\n'.join(s))
        pbsScript.flush()
        for line in s:
            self.logging.debug(" CONFIG: %s" % line)
        
        s = []
        s.append('#!/bin/sh');
        if self.workerNodeWorkDir:
            s.append('cd ' + self.workerNodeWorkDir)
        s.append('rm -fr $PBS_JOBCOOKIE')
        s.append('touch $HOME/done.$1')
        epilogue.write( '\n'.join( s ) )
        epilogue.flush()
        os.chmod( epilogue.name, 700 )
        self.logging.debug(" Beginning to qsub")
        qsubStart = time.time()
        p = subprocess.Popen("qsub %s" % pbsScript.name, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (child_stdout, child_stderr) = p.communicate()
        pbsScript.close()
        epilogue.close()
        qsubStop = time.time()
        self.logging.debug(" qsub ended: %s seconds" % (qsubStop - qsubStart))

        if p.returncode != 0:
            self.logging.error('Error in job submission')
            self.logging.error(child_stderr)
            raise SchedulerError('PBS error', child_stderr)
        
        jobid = child_stdout.strip()
        return {job['name']:jobid}, None, None 

    def query(self, obj, service='', objType='node') :
        """
        query status and eventually other scheduler related information
        It may use single 'node' scheduler id or bulk id for association
        """
        if type(obj) != Task :
            raise SchedulerError('wrong argument type', str( type(obj) ))

        jobids=[]
        for job in obj.jobs :
            if not self.valid( job.runningJob ): continue
            id=str(job.runningJob['schedulerId']).strip()
            p = subprocess.Popen( ['qstat', '-x', id], stdout=subprocess.PIPE,
                                                       stderr=subprocess.PIPE)
            qstat_output, \
                qstat_error = p.communicate()
            qstat_return    = p.returncode

            if qstat_return:
                if qstat_return != 153: # 153 means the job isn't there
                    self.logging.error('Error in job query for '+id)
                    self.logging.error('PBS stdout: \n %s' % qstat_output)
                    self.logging.error('PBS stderr: \n %s' % qstat_error)
                    raise SchedulerError('PBS error', '%s: %s' % (qstat_error, qstat_return) )
        
            host=''
            if len(qstat_output)==0:
                pbs_stat='Done'
            else:
                if qstat_output.find('</exec_host>') >= 0:
                    host = qstat_output[ qstat_output.find('<exec_host>') + len('<exec_host>') :
                                         qstat_output.find('</exec_host>') ]
                if qstat_output.find('</job_state>') >= 0:
                    pbs_stat = qstat_output[ qstat_output.find('<job_state>') + len('<job_state>') :
                                             qstat_output.find('</job_state>') ]

            job.runningJob['statusScheduler']=pbs_stat
            job.runningJob['status'] = self.status_map[pbs_stat]
            job.runningJob['destination']=host
            
    def kill(self, obj):

        for job in obj.jobs :
            if not self.valid( job.runningJob ): continue
            id=str(job.runningJob['schedulerId']).strip()

            p = subprocess.Popen( ['qdel', id], stdout=subprocess.PIPE,
                                                       stderr=subprocess.STDOUT)
            qdel_output, \
                qdel_error = p.communicate()
            qdel_return    = p.returncode

            if qdel_return != 0:
                self.logging.error('Error in job kill for '+id)
                self.logging.error('PBS Error stdout: %s' % qdel_output)
                raise SchedulerError('PBS Error in kill', qdel_output)                  
        
    def getOutput( self, obj, outdir='' ):
        """
        retrieve output or just put it in the destination directory

        does not return
        """
        pass