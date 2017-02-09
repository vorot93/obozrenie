FROM ubuntu
MAINTAINER Artem Vorotnikov <artem@vorotnikov.me>

RUN apt update && \
    apt install -y python3-pip python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 gnome-icon-theme gnome-icon-theme-symbolic geoip-database qstat && \
    ln -sf /usr/bin/quakestat /usr/bin/qstat

RUN dpkg-reconfigure locales && \
    locale-gen en_US.UTF-8 && \
    locale-gen ru_RU.UTF-8

ADD . /usr/src/app
WORKDIR /usr/src/app
RUN pip3 install -r requirements.txt
RUN /usr/src/app/compile_translations.sh

RUN export uid=1000 gid=1000 && \
    mkdir -p /home/obozrenie && \
    echo "obozrenie:x:${uid}:${gid}:obozrenie,,,:/home/obozrenie:/bin/bash" >> /etc/passwd && \
    echo "obozrenie:x:${uid}:" >> /etc/group && \
    mkdir -p /etc/sudoers.d && \
    echo "obozrenie ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/obozrenie && \
    chmod 0440 /etc/sudoers.d/obozrenie && \
    chown ${uid}:${gid} -R /home/obozrenie

USER obozrenie
ENV HOME /home/obozrenie
ENTRYPOINT [ "/usr/src/app/obozrenie-gtk" ]
