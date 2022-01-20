# Parallel Hadd
This is a python package to allow usage of hadd (from ROOT CERN) in parallel. It allows adding histogram and `.root` files. The addition will occur in blocks instead of one at all. It was designed to work with limited memory (local development primilary). The addition will use `multiprocessing` if avilable in your python enviroment. 

## Requirements:
The minimum supported python version is 3.6 and 3.7 and 3.8 are tested. Not tested on python 3.9 yet. 


## Install 

## from pypi

you can install the package directly from pypi repository
```
pip install parallel-hadd
```

## build manually
1. clone this repository

```
git clone https://github.com/MohamedElashri/hadd-parallel
```

2. build the package from source using `pip`

```
pip3 install ./hadd-parallel
```

## Manual

The command to use the package is 

```
phadd
```

you can use it as the following (with default arguments)

```
phadd out.root *.root
```

where `out.root` will be the output file the contains all the `*.root` files. 

more information about how to customize the settings are available on program help page

```
phadd -h
```


## Help page

```
usage: phadd [-h] [-t TMPDIR] [-j NUM_JOBS] [-n NUM_FILES] [-f] [-s] [-l {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [-v]
             output_file input_files [input_files ...]

Hadd ROOT histograms in parallel

positional arguments:
  output_file           the output file
  input_files           one or more input files

optional arguments:
  -h, --help            show this help message and exit
  -t TMPDIR, --tmpdir TMPDIR
                        the temporary directory to store intermediate files
  -j NUM_JOBS, --jobs NUM_JOBS
                        the number of jobs to run in parallel , [default cpu_count * 2 = {N_JOBS}
  -n NUM_FILES, --num_files NUM_FILES
                        the number of files to hadd at once
  -f, --force_overwrite
                        force overwrite of output file
  -s, --save-tmp        save the intermediate files, otherwise they will be deleted (which is the default)
  -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}, --log {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        the log level, [default Warning]
  -v, --version         show program's version number and exit

```

